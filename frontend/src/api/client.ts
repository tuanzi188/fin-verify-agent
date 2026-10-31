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

export type ConversationTurn = components['schemas']['ConversationTurn']
export type ConversationSummary = components['schemas']['ConversationSummary']
export type ConversationDetail = components['schemas']['ConversationDetail']
export type ConversationListResponse = components['schemas']['ConversationListResponse']

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
  result_ids?: string[]
  new_result_count?: number | null
  truncated?: boolean
  table_id?: string | null
  row_indices?: number[]
  column_indices?: number[]
  result_chars?: number
  payload_chars?: number
  evidence_chars_added?: number
  duplicate_skipped?: number
  policy?: string | null
  error_detail?: string | null
}

// ---------------------------------------------------------------------------
// 金融查证契约
// ---------------------------------------------------------------------------

export type EvidenceKind = 'paragraph' | 'table_rows' | 'table_cell' | 'document'

export interface SourceSpan {
  source_id: string
  kind: 'section' | 'table' | string
  page?: number | null
  start?: number | null
  end?: number | null
  chars?: number | null
}

export interface EvidenceTableCell {
  column: string
  column_index: number
  value?: string | null
}

export interface EvidenceTableRow {
  row: number
  row_name: string
  cells: EvidenceTableCell[]
}

export interface TablePreview {
  table_id: string
  caption: string
  page?: number | null
  page_end?: number | null
  unit: string
  unit_source: string
  footnote: string
  columns: string[]
  display_columns: string[]
  column_indices: number[]
  rows: EvidenceTableRow[]
  row_indices: number[]
  total_rows: number
}

export interface EvidenceRef {
  evidence_id: string
  doc_id: string
  doc_title: string
  company: string
  period: string
  doc_type: string
  kind: EvidenceKind
  page?: number | null
  section_id?: string | null
  path: string[]
  table_id?: string | null
  table_caption?: string | null
  row_indices: number[]
  column_indices: number[]
  row_name?: string | null
  column?: string | null
  cell_text?: string | null
  unit: string
  footnote: string
  content: string
  source_spans: SourceSpan[]
  table_preview?: TablePreview | null
  verified: boolean
  note?: string | null
}

export interface FinanceCitation {
  marker: number
  evidence: EvidenceRef
}

export interface CalcRecord {
  formula: string
  inputs: Record<string, unknown>
  result: unknown
  unit?: string
  tolerance?: number | null
  note?: string
}

export interface Claim {
  text: string
  citation_markers: number[]
  calc_index?: number | null
  uncertainty?: string | null
}

export interface FinanceChatResponse {
  status: AgentStatus
  answer: string
  claims: Claim[]
  citations: FinanceCitation[]
  calc: CalcRecord[]
  trace: AgentTraceStep[]
  conversation_id: string
  model_used: string
  variant: string
  strategy: 'structured_rag' | 'agent' | string
  route_reason: string
  search_mode: string
  duration_ms: number
  token_usage?: Record<string, number> | null
  notes: string[]
}

export interface FinanceStats {
  documents: number
  companies: number
  periods: string[]
  sections: number
  tables: number
  blocks: number
  prose_blocks: number
  table_blocks: number
  chunks?: number | null
  index_fingerprint?: string | null
  index_version?: string | null
  corpus_ready: boolean
  company_names: string[]
}

export interface FinanceDocumentInfo {
  doc_id: string
  title: string
  company: string
  period: string
  doc_type: string
  code: string
  unit: string
  currency?: string
  announcement_date?: string | null
  published_at?: string | null
  source_url?: string | null
  page_start?: number | null
  page_end?: number | null
  sections: number
  tables: number
  status: string
}

export async function sendFinanceChat(
  query: string,
  conversationId?: string,
  maxSteps = 10,
  searchMode: 'sections' | 'blocks' = 'blocks',
): Promise<FinanceChatResponse> {
  const { data } = await api.post<FinanceChatResponse>('/finance/chat', {
    query,
    conversation_id: conversationId,
    max_steps: maxSteps,
    search_mode: searchMode,
    // 产品默认自动选路；UI 不暴露固定窗 RAG 或 Agent/RAG 开关。
    strategy: 'auto',
  })
  return data
}

export async function getFinanceStats(): Promise<FinanceStats> {
  const { data } = await api.get<FinanceStats>('/finance/stats')
  return data
}

export async function getFinanceDocuments(): Promise<{ documents: FinanceDocumentInfo[] }> {
  const { data } = await api.get<{ documents: FinanceDocumentInfo[] }>('/finance/documents')
  return data
}

export async function getFinanceEvidence(options: {
  evidenceId?: string
  tableId?: string
  rows?: string
  columns?: string
}): Promise<EvidenceRef> {
  const { data } = await api.get<{ evidence: EvidenceRef }>('/finance/evidence', {
    params: {
      evidence_id: options.evidenceId,
      table_id: options.tableId,
      rows: options.rows,
      columns: options.columns,
    },
  })
  return data.evidence
}

export interface LabArmSummary {
  name: string
  label?: string
  pass_at_1?: number | null
  pass_pow_k?: number | null
  tasks: number
  runs: number
  tokens_per_run?: number | null
  median_latency_ms?: number | null
  latency_mean_ms?: number | null
  avg_steps?: number | null
  hard_gates: Record<string, unknown>
  manifest: Record<string, unknown>
}

export interface LabDimensionSummary {
  name: string
  tasks: number
  baseline_pass_at_1?: number | null
  improved_pass_at_1?: number | null
  delta?: number | null
  delta_exceeds_ci_width?: boolean | null
}

export interface LabRunSummary {
  available: boolean
  comparison_kind: string
  left_label: string
  right_label: string
  generated_at?: string | null
  verdict?: string | null
  metric?: string | null
  tasks_compared: number
  arms: LabArmSummary[]
  dimensions: LabDimensionSummary[]
  mcnemar: Record<string, unknown>
  optional_baseline?: LabArmSummary | null
  optional_baseline_label?: string | null
  artifact?: string | null
  compare_artifact?: string | null
  message?: string | null
}

export async function getLabRuns(): Promise<LabRunSummary> {
  const { data } = await api.get<LabRunSummary>('/lab/runs')
  return data
}

export async function getHealth(): Promise<{ status: string; service?: string }> {
  const { data } = await api.get('/health')
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

export async function queryAuditLogs(request: AuditLogQueryRequest): Promise<AuditLogQueryResponse> {
  const { data } = await api.post<AuditLogQueryResponse>('/analytics/audit', request)
  return data
}
