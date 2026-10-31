import { useState, useCallback, useEffect, useRef } from 'react'
import { getConversations, getConversation, deleteConversation, ConversationSummary } from '../api/client'
import type { HistoryTurn } from './useChat'

export interface UseConversationsReturn {
  conversations: ConversationSummary[]
  conversationsLoading: boolean
  activeConversationId: string | undefined
  setActiveConversationId: (id: string | undefined) => void
  loadConversations: () => Promise<void>
  loadConversation: (convId: string) => Promise<HistoryTurn[]>
  deleteConv: (convId: string) => Promise<void>
  startNewConversation: () => void
}

export function useConversations(): UseConversationsReturn {
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [conversationsLoading, setConversationsLoading] = useState(false)
  const [activeConversationId, setActiveConversationId] = useState<string | undefined>()

  const loadConversations = useCallback(async () => {
    setConversationsLoading(true)
    try {
      const result = await getConversations()
      // 兜底：接口返回结构异常时不至于让整页因 undefined 而白屏
      setConversations(result?.conversations ?? [])
    } catch (err) {
      console.error('加载对话列表失败:', err)
    } finally {
      setConversationsLoading(false)
    }
  }, [])

  const loadConversation = useCallback(async (convId: string): Promise<HistoryTurn[]> => {
    const result = await getConversation(convId)
    setActiveConversationId(convId)
    return result.turns.map((turn) => ({
      query: turn.query,
      answer: turn.answer,
      timestamp: turn.timestamp,
      // 结构化查证结果随会话返回；旧轮次为 null，由 loadHistoryMessages 退回纯文本。
      payload: turn.payload ?? undefined,
    }))
  }, [])

  const deleteConv = useCallback(async (convId: string) => {
    await deleteConversation(convId)
    if (activeConversationId === convId) {
      setActiveConversationId(undefined)
    }
    await loadConversations()
  }, [activeConversationId, loadConversations])

  const startNewConversation = useCallback(() => {
    setActiveConversationId(undefined)
  }, [])

  useEffect(() => {
    loadConversations()
  }, [loadConversations])

  return {
    conversations,
    conversationsLoading,
    activeConversationId,
    setActiveConversationId,
    loadConversations,
    loadConversation,
    deleteConv,
    startNewConversation,
  }
}