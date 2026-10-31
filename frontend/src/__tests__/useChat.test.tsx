import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useChat } from '../hooks/useChat'

const mocks = vi.hoisted(() => ({
  sendFinanceChat: vi.fn(),
}))

vi.mock('../api/client', () => ({
  sendFinanceChat: mocks.sendFinanceChat,
  extractErrorMessage: (error: any, fallback: string) =>
    error?.response?.data?.detail || error?.message || fallback,
}))

describe('useChat finance mode', () => {
  beforeEach(() => {
    mocks.sendFinanceChat.mockReset()
  })

  it('keeps structured finance claims, citations and calc records', async () => {
    mocks.sendFinanceChat.mockResolvedValue({
      status: 'answered',
      answer: '智能家居收入占比 65.71% [1]。',
      claims: [
        {
          text: '智能家居收入占比 65.71%',
          citation_markers: [1],
          calc_index: 0,
          uncertainty: null,
        },
      ],
      citations: [
        {
          marker: 1,
          evidence: {
            evidence_id: 'doc#t0013#rows#1#cols#0,2',
            doc_id: 'doc',
            doc_title: '美的集团2025年年度报告',
            company: '美的集团',
            period: '2025',
            doc_type: 'annual_report',
            kind: 'table_rows',
            page: 58,
            table_id: 'doc#t0013',
            row_indices: [1],
            column_indices: [2],
            content: '表 智能家居',
            source_spans: [],
            verified: true,
          },
        },
      ],
      calc: [
        {
          formula: 'a / b * 100',
          inputs: { a: '299927239', b: '456451731' },
          result: '65.7084',
          unit: '%',
        },
      ],
      trace: [],
      conversation_id: 'conv-finance',
      model_used: 'deepseek-chat',
      variant: 'improved',
      strategy: 'structured_rag',
      route_reason: '单公司/单期间/单指标的查数，一次结构检索即可',
      search_mode: 'blocks',
      duration_ms: 1200,
      notes: [],
    })

    const { result } = renderHook(() => useChat())

    await act(async () => {
      await result.current.sendMessage('美的智能家居收入占比')
    })

    expect(mocks.sendFinanceChat).toHaveBeenCalledWith('美的智能家居收入占比', undefined)
    expect(result.current.messages).toHaveLength(2)
    expect(result.current.messages[1].finance?.claims).toHaveLength(1)
    expect(result.current.messages[1].finance?.calc[0].result).toBe('65.7084')
    expect(result.current.messages[1].finance?.citations[0].evidence.table_id).toBe('doc#t0013')
    expect(result.current.conversationId).toBe('conv-finance')
    expect(result.current.loading).toBe(false)
  })

  it('appends an error message when the request fails', async () => {
    mocks.sendFinanceChat.mockRejectedValue({ response: { status: 500, data: { detail: 'boom' } } })

    const { result } = renderHook(() => useChat())

    await act(async () => {
      await result.current.sendMessage('毛利率是多少')
    })

    expect(result.current.messages).toHaveLength(2)
    expect(result.current.messages[1].status).toBe('error')
    expect(result.current.messages[1].content).toContain('boom')
    expect(result.current.loading).toBe(false)
  })

  it('restores structured finance turns from conversation history', () => {
    const { result } = renderHook(() => useChat())

    act(() => {
      result.current.loadHistoryMessages([
        {
          query: '美的智能家居收入占比',
          answer: '智能家居收入占比 65.71% [1]。',
          timestamp: 1700000000000,
          payload: {
            kind: 'finance',
            status: 'answered',
            strategy: 'agent',
            route_reason: '多步查证',
            claims: [{ text: '占比 65.71%', citation_markers: [1], calc_index: 0 }],
            citations: [
              {
                marker: 1,
                evidence: {
                  evidence_id: 'doc#t0013#rows#5#cols#2',
                  doc_id: 'doc',
                  doc_title: '美的集团2025年年度报告',
                  kind: 'table_rows',
                  table_id: 'doc#t0013',
                  row_indices: [5],
                  column_indices: [2],
                  unit: '千元',
                  verified: true,
                },
              },
            ],
            calc: [{ formula: 'a/b*100', inputs: {}, result: '65.71', unit: '%' }],
            trace: [],
            notes: [],
          },
        },
        // 旧会话：没有 payload，退回纯文本。
        { query: '旧问题', answer: '旧回答', timestamp: 1700000001000 },
      ])
    })

    expect(result.current.messages).toHaveLength(4)
    const restored = result.current.messages[1]
    expect(restored.status).toBe('answered')
    expect(restored.finance?.claims).toHaveLength(1)
    expect(restored.finance?.citations[0].evidence.table_id).toBe('doc#t0013')
    expect(restored.finance?.calc[0].unit).toBe('%')
    expect(result.current.messages[3].finance).toBeUndefined()
  })
})
