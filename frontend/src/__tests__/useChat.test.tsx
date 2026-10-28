import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChat } from '../hooks/useChat'

const mocks = vi.hoisted(() => ({
  sendChat: vi.fn(),
  sendChatStream: vi.fn(),
}))

vi.mock('../api/client', () => ({
  sendChat: mocks.sendChat,
  sendChatStream: mocks.sendChatStream,
  extractErrorMessage: (error: any, fallback: string) =>
    error?.response?.data?.detail || error?.message || fallback,
}))

describe('useChat agent mode', () => {
  beforeEach(() => {
    mocks.sendChat.mockReset()
    mocks.sendChatStream.mockReset()
  })

  it('appends assistant message with status, trace and conversation id', async () => {
    mocks.sendChat.mockResolvedValue({
      answer: '住宿标准为每晚不超过五百元 [1]。',
      status: 'answered',
      citations: [
        { document_id: 'd2', document_title: '差旅与报销制度', content: '住宿标准...' },
      ],
      trace: [
        { step: 1, tool: 'search_kb', ok: true, result_count: 2, latency_ms: 30 },
      ],
      conversation_id: 'conv-1',
      model_used: 'deepseek-chat',
      reasoning_mode_used: 'agent-flash',
    })

    const { result } = renderHook(() => useChat())

    await act(async () => {
      await result.current.sendMessage('出差住宿标准是多少？', { mode: 'agent' })
    })

    expect(mocks.sendChat).toHaveBeenCalledWith('出差住宿标准是多少？', undefined, 'agent')
    expect(result.current.messages).toHaveLength(2)
    expect(result.current.messages[1].role).toBe('assistant')
    expect(result.current.messages[1].status).toBe('answered')
    expect(result.current.messages[1].trace).toHaveLength(1)
    expect(result.current.conversationId).toBe('conv-1')
    expect(result.current.loading).toBe(false)
  })

  it('renders an error assistant message when the request fails', async () => {
    mocks.sendChat.mockRejectedValue({ response: { status: 500, data: { detail: 'boom' } } })

    const { result } = renderHook(() => useChat())

    await act(async () => {
      await result.current.sendMessage('住宿标准', { mode: 'agent' })
    })

    expect(result.current.messages).toHaveLength(2)
    expect(result.current.messages[1].status).toBe('error')
    expect(result.current.messages[1].content).toContain('boom')
    expect(result.current.loading).toBe(false)
  })
})
