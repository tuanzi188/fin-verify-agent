import { useState, useCallback, useRef, useEffect } from 'react'
import { message } from 'antd'
import {
  sendChatStream,
  sendChat,
  DisplayCitation,
  AgentStatus,
  AgentTraceStep,
  extractErrorMessage,
} from '../api/client'

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations?: DisplayCitation[]
  confidence?: number
  model?: string
  mode?: string
  reasoning_content?: string
  isFallback?: boolean
  // agent 模式专有：四态 + 执行轨迹。RAG 消息保持 undefined。
  status?: AgentStatus
  trace?: AgentTraceStep[]
  timestamp: number
}

export interface SendOptions {
  mode?: 'rag' | 'agent'
}

export interface UseChatReturn {
  messages: Message[]
  loading: boolean
  streamingContent: string
  conversationId: string | undefined
  setConversationId: (id: string | undefined) => void
  setMessages: (messages: Message[]) => void
  sendMessage: (query: string, options?: SendOptions) => Promise<void>
  stopGeneration: () => void
  loadHistoryMessages: (turns: { query: string; answer: string; timestamp: number }[]) => void
  startNew: () => void
}

export function useChat(
  onConversationUpdate?: () => void,
): UseChatReturn {
  const [messages, setMessages] = useState<Message[]>([])
  const [loading, setLoading] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')
  const [conversationId, setConversationId] = useState<string | undefined>()
  const abortRef = useRef<AbortController | null>(null)
  const streamingRef = useRef('')
  const discardOnAbortRef = useRef(false)

  // 组件卸载时终止在途请求，避免卸载后继续 setState / 泄漏连接
  useEffect(() => {
    return () => {
      discardOnAbortRef.current = true
      abortRef.current?.abort()
      abortRef.current = null
    }
  }, [])

  const sendMessage = useCallback(async (query: string, options?: SendOptions) => {
    if (loading) return

    const userMsg: Message = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: query,
      timestamp: Date.now(),
    }
    setMessages(prev => [...prev, userMsg])

    // agent 模式：非流式。复用 /chat 入口，鉴权/会话/审计与 RAG 一致。
    if (options?.mode === 'agent') {
      setLoading(true)
      try {
        const result = await sendChat(query, conversationId, 'agent')
        const assistantMsg: Message = {
          id: `assistant-${Date.now()}`,
          role: 'assistant',
          content: result.answer || '（本轮没有返回内容）',
          citations: result.citations || [],
          model: result.model_used,
          mode: result.reasoning_mode_used || 'agent',
          status: (result.status || 'error') as AgentStatus,
          trace: result.trace || [],
          timestamp: Date.now(),
        }
        setMessages(prev => [...prev, assistantMsg])
        setConversationId(result.conversation_id)
        onConversationUpdate?.()
      } catch (err) {
        console.error('Agent 请求失败:', err)
        const detail = extractErrorMessage(err, '请求失败，请检查网络连接或稍后重试')
        const code = (err as any)?.response?.status
        message.error(code ? `[${code}] ${detail}` : detail)
        setMessages(prev => [...prev, {
          id: `error-${Date.now()}`,
          role: 'assistant',
          content: `抱歉，深度检索执行失败：${detail}`,
          status: 'error',
          trace: [],
          timestamp: Date.now(),
        }])
      } finally {
        setLoading(false)
      }
      return
    }

    setLoading(true)
    setStreamingContent('')
    streamingRef.current = ''
    discardOnAbortRef.current = false
    let receivedDone = false

    const abortController = new AbortController()
    abortRef.current = abortController

    try {
      await sendChatStream(
        query,
        (token) => {
          streamingRef.current += token
          setStreamingContent(streamingRef.current)
        },
        (result) => {
          receivedDone = true
          const assistantMsg: Message = {
            id: `assistant-${Date.now()}`,
            role: 'assistant',
            content: result.answer,
            citations: result.citations,
            confidence: result.confidence,
            model: result.model_used,
            mode: result.reasoning_mode_used,
            reasoning_content: result.reasoning_content,
            isFallback: result.is_fallback,
            timestamp: Date.now(),
          }
          setMessages(prev => [...prev, assistantMsg])
          streamingRef.current = ''
          setStreamingContent('')
          setConversationId(result.conversation_id)
          onConversationUpdate?.()
        },
        (error, code) => {
          console.error('流式请求失败:', error, code)
          const detail = extractErrorMessage(error, '请求失败，请检查网络连接或稍后重试')
          const userMsg = code ? `[${code}] ${detail}` : detail
          message.error(userMsg)
          const errMsg: Message = {
            id: `error-${Date.now()}`,
            role: 'assistant',
            content: `抱歉，请求处理失败：${userMsg}`,
            timestamp: Date.now(),
          }
          setMessages(prev => [...prev, errMsg])
          streamingRef.current = ''
          setStreamingContent('')
        },
        conversationId,
        abortController.signal,
      )

      // 流正常结束但没收到 done（网络中断 / 代理断开 / 后端异常退出）：
      // 保留已输出的部分内容并提示，而不是静默丢弃。
      if (!receivedDone && !abortController.signal.aborted && streamingRef.current.trim()) {
        setMessages(prev => [...prev, {
          id: `partial-${Date.now()}`,
          role: 'assistant',
          content: `${streamingRef.current}\n\n> 连接中断，以上为已生成的部分内容。`,
          timestamp: Date.now(),
        }])
        streamingRef.current = ''
        setStreamingContent('')
      }
    } catch (err: any) {
      if (err?.name === 'AbortError') {
        // 用户主动停止：保留已流式输出的文本；切换会话/卸载导致的 abort 直接丢弃
        if (!discardOnAbortRef.current && streamingRef.current.trim()) {
          const stoppedMsg: Message = {
            id: `stopped-${Date.now()}`,
            role: 'assistant',
            content: streamingRef.current,
            timestamp: Date.now(),
          }
          setMessages(prev => [...prev, stoppedMsg])
        }
        streamingRef.current = ''
        setStreamingContent('')
      } else {
        console.error('发送消息失败:', err)
        const detail = extractErrorMessage(err, '请求失败，请检查网络连接或稍后重试')
        const code = err?.response?.status
        const userMsg = code ? `[${code}] ${detail}` : detail
        message.error(userMsg)
        const errMsg: Message = {
          id: `error-${Date.now()}`,
          role: 'assistant',
          content: `抱歉，请求处理失败：${userMsg}`,
          timestamp: Date.now(),
        }
        setMessages(prev => [...prev, errMsg])
        streamingRef.current = ''
        setStreamingContent('')
      }
    } finally {
      setLoading(false)
      abortRef.current = null
      discardOnAbortRef.current = false
    }
  }, [loading, conversationId, onConversationUpdate])

  const stopGeneration = useCallback(() => {
    if (abortRef.current) {
      discardOnAbortRef.current = false
      abortRef.current.abort()
      abortRef.current = null
    }
  }, [])

  const loadHistoryMessages = useCallback(
    (turns: { query: string; answer: string; timestamp: number }[]) => {
      const msgs: Message[] = turns.flatMap((turn, index) => [
        {
          id: `user-${index}`,
          role: 'user' as const,
          content: turn.query,
          timestamp: turn.timestamp,
        },
        {
          id: `assistant-${index}`,
          role: 'assistant' as const,
          content: turn.answer,
          timestamp: turn.timestamp,
        },
      ])
      setMessages(msgs)
    },
    [],
  )

  const startNew = useCallback(() => {
    // 切换新会话时终止在途请求，避免旧流把回答写进新会话
    discardOnAbortRef.current = true
    abortRef.current?.abort()
    abortRef.current = null
    streamingRef.current = ''
    setMessages([])
    setConversationId(undefined)
    setStreamingContent('')
  }, [])

  return {
    messages,
    loading,
    streamingContent,
    conversationId,
    setConversationId,
    setMessages,
    sendMessage,
    stopGeneration,
    loadHistoryMessages,
    startNew,
  }
}