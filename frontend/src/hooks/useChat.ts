import { useState, useCallback } from 'react'
import { message } from 'antd'
import {
  sendFinanceChat,
  AgentStatus,
  AgentTraceStep,
  extractErrorMessage,
} from '../api/client'
import { parseFinancePayload } from '../utils/financeHistory'
import type { TurnStatus } from '../utils/status'

/** 金融查证消息的结构化载荷；定义在 utils 里，历史会话与实时响应共用同一形状。 */
export type { FinanceMessagePayload } from '../utils/financeHistory'
import type { FinanceMessagePayload } from '../utils/financeHistory'

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  model?: string
  /** 展示状态。比 AgentStatus 多一个 legacy：旧会话有答案但没保存证据。 */
  status?: TurnStatus
  trace?: AgentTraceStep[]
  finance?: FinanceMessagePayload
  timestamp: number
}

export interface HistoryTurn {
  query: string
  answer: string
  timestamp: number
  /** 旧会话没有该字段；有 payload 时按金融结论卡还原。 */
  payload?: unknown
}

export interface UseChatReturn {
  messages: Message[]
  loading: boolean
  conversationId: string | undefined
  setConversationId: (id: string | undefined) => void
  setMessages: (messages: Message[]) => void
  sendMessage: (query: string) => Promise<void>
  loadHistoryMessages: (turns: HistoryTurn[]) => void
  startNew: () => void
}

export function useChat(onConversationUpdate?: () => void): UseChatReturn {
  const [messages, setMessages] = useState<Message[]>([])
  const [loading, setLoading] = useState(false)
  const [conversationId, setConversationId] = useState<string | undefined>()

  const sendMessage = useCallback(async (query: string) => {
    if (loading) return

    const userMsg: Message = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: query,
      timestamp: Date.now(),
    }
    setMessages((prev) => [...prev, userMsg])
    setLoading(true)
    try {
      const result = await sendFinanceChat(query, conversationId)
      const assistantMsg: Message = {
        id: `assistant-${Date.now()}`,
        role: 'assistant',
        content: result.answer || '（本轮没有返回内容）',
        model: result.model_used,
        status: (result.status || 'error') as AgentStatus,
        trace: result.trace || [],
        finance: {
          status: (result.status || 'error') as AgentStatus,
          strategy: result.strategy || 'agent',
          route_reason: result.route_reason || '',
          claims: result.claims || [],
          citations: result.citations || [],
          calc: result.calc || [],
          trace: result.trace || [],
          notes: result.notes || [],
          verification: result.verification
            ? (result.verification as FinanceMessagePayload['verification'])
            : undefined,
          resolved_query: result.resolved_query,
        },
        timestamp: Date.now(),
      }
      setMessages((prev) => [...prev, assistantMsg])
      setConversationId(result.conversation_id)
      onConversationUpdate?.()
    } catch (err) {
      console.error('金融查证请求失败:', err)
      const detail = extractErrorMessage(err, '请求失败，请检查网络连接或稍后重试')
      const code = (err as { response?: { status?: number } })?.response?.status
      message.error(code ? `[${code}] ${detail}` : detail)
      setMessages((prev) => [
        ...prev,
        {
          id: `error-${Date.now()}`,
          role: 'assistant',
          content: `抱歉，本次查证执行失败：${detail}`,
          status: 'error',
          trace: [],
          timestamp: Date.now(),
        },
      ])
    } finally {
      setLoading(false)
    }
  }, [loading, conversationId, onConversationUpdate])

  const loadHistoryMessages = useCallback((turns: HistoryTurn[]) => {
    const msgs: Message[] = turns.flatMap((turn, index) => {
      const finance = parseFinancePayload(turn.payload)
      // 后端会话时间戳是秒；消息统一用毫秒，避免时间显示成 1970 年。
      const timestampMs = turn.timestamp < 1e12 ? turn.timestamp * 1000 : turn.timestamp
      const userMessage: Message = {
        id: `user-${index}`,
        role: 'user' as const,
        content: turn.query,
        timestamp: timestampMs,
      }
      // 有结构化载荷时走结论卡；没有就按 Markdown 还原，和旧版本行为一致。
      const assistantMessage: Message = finance
        ? {
            id: `assistant-${index}`,
            role: 'assistant' as const,
            content: turn.answer,
            status: finance.status,
            trace: finance.trace,
            finance,
            timestamp: timestampMs,
          }
        : {
            id: `assistant-${index}`,
            role: 'assistant' as const,
            content: turn.answer,
            // 旧会话没有结构化载荷：有答案，但引用和证据都没保存。
            // 标成 legacy 而不是让 normalizeStatus 落到 error——那不是执行失败。
            status: 'legacy',
            timestamp: timestampMs,
          }
      return [userMessage, assistantMessage]
    })
    setMessages(msgs)
  }, [])

  const startNew = useCallback(() => {
    setMessages([])
    setConversationId(undefined)
  }, [])

  return {
    messages,
    loading,
    conversationId,
    setConversationId,
    setMessages,
    sendMessage,
    loadHistoryMessages,
    startNew,
  }
}
