/// <reference types="vite/client" />
import axios from 'axios'
import type { components } from './generated'

const ACCESS_TOKEN_STORAGE = 'knowledge_mind_access_token'

export interface AuthenticatedProfile {
  userId: string
  roles: string[]
  department: string
}

function getAuthHeaders(): Record<string, string> {
  const accessToken = sessionStorage.getItem(ACCESS_TOKEN_STORAGE) || ''
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {}
}

export async function verifyAccessToken(accessToken: string): Promise<void> {
  await axios.get('/api/v1/auth/me', {
    headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
  })
  sessionStorage.setItem(ACCESS_TOKEN_STORAGE, accessToken)
}

export function clearAccessToken(): void {
  sessionStorage.removeItem(ACCESS_TOKEN_STORAGE)
}

export async function getAuthenticatedProfile(): Promise<AuthenticatedProfile> {
  const profileResponse = await api.get<AuthenticatedProfile>('/auth/me')
  return profileResponse.data
}

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
})

api.interceptors.request.use((config) => {
  const authHeaders = getAuthHeaders()
  Object.entries(authHeaders).forEach(([key, value]) => {
    config.headers.set(key, value)
  })
  return config
})

export function extractErrorMessage(err: unknown, fallback = '请求失败'): string {
  if (err && typeof err === 'object') {
    const error = err as { response?: { data?: { detail?: string } }; message?: string }
    if (error.response?.data?.detail) {
      return error.response.data.detail
    }
    if (error.message) {
      return error.message
    }
  }
  return fallback
}

export type Citation = components['schemas']['Citation']
export type ChatResponse = components['schemas']['ChatResponse']
export type ChatSegment = components['schemas']['ChatSegment']
export type SearchResult = components['schemas']['SearchResult']
export type SearchResponse = components['schemas']['SearchResponse']
export type UploadResponse = components['schemas']['DocumentUploadResponse']
export type ConversationTurn = components['schemas']['ConversationTurn']
export type ConversationSummary = components['schemas']['ConversationSummary']
export type ConversationDetail = components['schemas']['ConversationDetail']
export type ConversationListResponse = components['schemas']['ConversationListResponse']

/** Agent 模式的引用没有重排分：score 允许为空，不要编 0 让界面显示“匹配 0%”。 */
export type DisplayCitation = Omit<Citation, 'score'> & { score?: number | null }

export type AgentStatus = 'answered' | 'abstained' | 'clarified' | 'error'

export interface AgentTraceStep {
  step: number
  tool: string
  ok: boolean
  result_count?: number
  latency_ms?: number
  injected?: string | null
  error_kind?: string | null
  args?: Record<string, unknown>
  note?: string | null
}

export interface AgentChatResponse extends Omit<ChatResponse, 'citations'> {
  citations: DisplayCitation[]
  status?: AgentStatus | null
  trace?: AgentTraceStep[] | null
  variant?: string | null
  duration_ms?: number | null
}

/**
 * 非流式问答。mode=agent 时走工具循环（单轮、不可流式）。
 * 复用 /chat 入口：鉴权、会话隔离、审计与 RAG 路径完全一致。
 */
export async function sendChat(
  query: string,
  conversationId?: string,
  mode: 'rag' | 'agent' = 'rag',
  maxSteps = 3,
): Promise<AgentChatResponse> {
  const response = await api.post<AgentChatResponse>('/chat', {
    query,
    conversation_id: conversationId,
    mode,
    max_steps: maxSteps,
  })
  return response.data
}

export interface DocumentInfo {
  document_id: string
  filename: string
  title?: string | null
  author?: string | null
  department?: string | null
  classification?: string | null
  file_type: string
  file_size: number
  upload_time?: string | null
  chunks_count: number
  status?: string
  owner?: string | null
  is_public?: boolean
  allowed_users?: string[]
  allowed_departments?: string[]
  allowed_roles?: string[]
}

export interface AclOptions {
  isPublic?: boolean
  allowedUsers?: string
  allowedDepartments?: string
  allowedRoles?: string
}

export async function uploadDocument(file: File, title?: string, acl?: AclOptions): Promise<UploadResponse> {
  const form = new FormData()
  form.append('file', file)
  if (title) form.append('title', title)
  if (acl?.isPublic !== undefined) form.append('is_public', String(acl.isPublic))
  if (acl?.allowedUsers) form.append('allowed_users', acl.allowedUsers)
  if (acl?.allowedDepartments) form.append('allowed_departments', acl.allowedDepartments)
  if (acl?.allowedRoles) form.append('allowed_roles', acl.allowedRoles)
  const { data } = await api.post<UploadResponse>('/upload', form)
  return data
}

export async function getStats(): Promise<{ total_chunks: number; status: string }> {
  const { data } = await api.get('/stats')
  return data
}

export async function getConversations(): Promise<ConversationListResponse> {
  const { data } = await api.get('/conversations')
  return data
}

export async function getConversation(conversationId: string): Promise<ConversationDetail> {
  const { data } = await api.get(`/conversations/${conversationId}`)
  return data
}

export async function deleteConversation(conversationId: string): Promise<{ success: boolean }> {
  const { data } = await api.delete(`/conversations/${conversationId}`)
  return data
}

export async function listDocuments(): Promise<{ documents: DocumentInfo[] }> {
  const { data } = await api.get('/documents')
  return data
}

export async function deleteDocument(documentId: string): Promise<{ status: string }> {
  const { data } = await api.delete(`/documents/${documentId}`)
  return data
}

export interface AnalyticsTimeRange {
  start?: string
  end?: string
}

export interface AnalyticsRequest {
  time_range?: AnalyticsTimeRange
  group_by?: string
  top_k?: number
}

export interface AnalyticsResponse {
  metric: string
  summary: Record<string, unknown>
  series: Record<string, unknown>[]
  detail?: Record<string, unknown> | null
  generated_at: string
}

export interface AuditLogQueryRequest {
  user_id?: string
  action?: string
  resource_type?: string
  resource_id?: string
  status?: string
  start_time?: string
  end_time?: string
  limit?: number
  offset?: number
}

export interface AuditLogQueryResponse {
  total: number
  logs: Record<string, unknown>[]
}

export async function getAnalyticsOverview(): Promise<AnalyticsResponse> {
  const { data } = await api.get<AnalyticsResponse>('/analytics/overview')
  return data
}

export async function analyzeDocuments(request?: AnalyticsRequest): Promise<AnalyticsResponse> {
  const { data } = await api.post<AnalyticsResponse>('/analytics/documents', request ?? {})
  return data
}

export async function analyzeConversations(request?: AnalyticsRequest): Promise<AnalyticsResponse> {
  const { data } = await api.post<AnalyticsResponse>('/analytics/conversations', request ?? {})
  return data
}

export async function analyzeTasks(request?: AnalyticsRequest): Promise<AnalyticsResponse> {
  const { data } = await api.post<AnalyticsResponse>('/analytics/tasks', request ?? {})
  return data
}

export async function analyzeGraph(request?: AnalyticsRequest): Promise<AnalyticsResponse> {
  const { data } = await api.post<AnalyticsResponse>('/analytics/graph', request ?? {})
  return data
}

export async function queryAuditLogs(request: AuditLogQueryRequest): Promise<AuditLogQueryResponse> {
  const { data } = await api.post<AuditLogQueryResponse>('/analytics/audit', request)
  return data
}

export interface StreamDoneResult {
  conversation_id: string
  citations: Citation[]
  confidence: number
  model_used: string
  reasoning_mode_used: string
  reasoning_content?: string
  is_fallback: boolean
  answer: string
}

export async function sendChatStream(
  query: string,
  onToken: (token: string) => void,
  onDone: (result: StreamDoneResult) => void,
  onError: (error: Error, code?: number) => void,
  conversationId?: string,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch('/api/v1/chat/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...getAuthHeaders(),
    },
    body: JSON.stringify({
      query,
      conversation_id: conversationId,
      top_k: 5,
      use_fallback: true,
    }),
    signal,
  })

  if (!response.ok) {
    let detail = `HTTP error: ${response.status}`
    try {
      const data = await response.json()
      if (data.detail) {
        detail = data.detail
      }
    } catch {
      // 非 JSON 响应，使用默认文案
    }
    throw new Error(detail)
  }

  const reader = response.body?.getReader()
  if (!reader) {
    throw new Error('Response body is not readable')
  }

  const decoder = new TextDecoder()
  let fullAnswer = ''
  let buffer = ''
  let finished = false

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed.startsWith('data: ')) continue

        const jsonStr = trimmed.slice(6)
        try {
          const event = JSON.parse(jsonStr)

          if (event.type === 'token') {
            fullAnswer += event.content
            onToken(event.content)
          } else if (event.type === 'done') {
            onDone({
              conversation_id: event.conversation_id,
              citations: event.citations || [],
              confidence: event.confidence_score || 0,
              model_used: event.model_used || '',
              reasoning_mode_used: '',
              reasoning_content: event.reasoning_content || '',
              is_fallback: event.is_fallback || false,
              answer: fullAnswer,
            })
            finished = true
          } else if (event.type === 'error') {
            onError(new Error(event.content), event.code)
          }
        } catch (e) {
          console.warn('SSE line parse failed:', e)
        }
      }

      // 收到 done 即结束读取，不再只依赖服务端关流——否则经 keep-alive 代理时
      // 连接不关闭，UI 会一直停在「正在生成」状态。
      if (finished) break
    }
  } finally {
    reader.releaseLock()
  }
}
