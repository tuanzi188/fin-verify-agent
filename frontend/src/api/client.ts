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

export type AgentStatus = 'answered' | 'abstained' | 'clarified' | 'needs_review' | 'error'

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
  /** 表级口径：consolidated / parent */
  statement_scope?: string
  /** 表级报表类型：balance_sheet / income_statement / cash_flow / ... */
  statement_type?: string
  content: string
  /** 逐字原文摘录：必须能在 content / 表格行里逐字找到。旧历史载荷可能没有。 */
  quote?: string
  /** quote 是否通过逐字校验。False 时只能当线索。 */
  quote_verified?: boolean
  /** 同一份文件内 quote 出现的次数；跨文件不去重。 */
  quote_occurrences?: number
  /** 同文件内重复且 locator 不足以区分落点。 */
  quote_ambiguous?: boolean
  /** 可选定位：PDF 用 page，XLSX 用 sheet/cell，DOCX 用 paragraph。 */
  locator?: Record<string, unknown>
  source_spans: SourceSpan[]
  table_preview?: TablePreview | null
  verified: boolean
  note?: string | null
  source_url?: string | null
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
  calculation_id?: string
  result_scale?: string
  input_sources?: Record<string, { evidence_id: string; table_id?: string; row?: number; column?: number; cell_text?: string; marker?: number }>
  grounding_status?: string
  recomputed?: boolean
}

export interface Claim {
  text: string
  citation_markers: number[]
  calc_index?: number | null
  uncertainty?: string | null
}

export interface MarketBriefSource {
  label: string
  domain: string
  url: string
  title: string
  published_at: string | null
  tier: number
  /** 来源摘要原文：用来核对综述那句话是否真由这条来源支撑。 */
  snippet?: string
}

/**
 * 联网综述（未核实）。与查证回答严格分开：
 * - summary/分项 是各方说法，不带 [n] 引用；
 * - disclaimer 由服务端追加；
 * - warnings 是数字溯源与来源名校验的结果。
 */
export interface MarketBrief {
  ok: boolean
  question: string
  summary: string
  sections: Record<string, string[]>
  misreadings: string[]
  divergences: string[]
  gaps: string[]
  sources: MarketBriefSource[]
  /** 检索到但被白名单 / 黑名单 / 缺日期丢掉的来源，带原因。 */
  skipped_sources?: Array<{ url: string; reason: string }>
  ingest_candidates?: Array<{ url: string; domain: string; title: string; published_at: string | null }>
  raw?: string
  as_of: string
  disclaimer: string
  warnings: string[]
  /** 实际发给检索服务的查询词，便于用户复核「到底搜了什么」。 */
  queries?: string[]
  skipped_reason?: string | null
  error?: string | null
  token_usage?: Record<string, number>
}

/** 概念问答实际检索到的公开信息。只做透明展示，不是可回查的证据。 */
export interface WebSourceRef {
  label: string
  domain: string
  title: string
  url: string
  published_at: string
}

/** 拒答分级（六类之一）。level 1 补条件可继续 / 2 产品边界 / 3 证据缺口。 */
export interface RefusalInfo {
  code: string
  label: string
  level: number
  level_label: string
  action: 'clarify' | 'refuse' | string
  action_label: string
  recover_by: 'user' | 'corpus' | 'manual' | 'none' | string
  hint: string
  detail?: string
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
  strategy: 'structured_rag' | 'agent' | 'market_brief' | 'concept' | string
  route_reason: string
  search_mode: string
  duration_ms: number
  token_usage?: Record<string, number> | null
  notes: string[]
  verification?: Record<string, unknown>
  resolved_query?: string
  market_brief?: MarketBrief | null
  refusal?: RefusalInfo | null
  /** 概念问答：首段定论（answer 里仍保留全文）。 */
  answer_lead?: string
  /** 概念问答实际检索到的公开信息。 */
  web_sources?: WebSourceRef[]
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
  origin?: 'corpus' | 'upload' | string
  pipeline_version?: string
}

export interface FinanceDocumentUploadResult {
  doc_id: string
  filename: string
  status: string
  sections: number
  tables: number
  raw_chars: number
  pipeline_version?: string
  warnings: string[]
  message: string
}

export async function uploadFinanceDocument(
  file: File,
  meta: { company?: string; period?: string; docType?: string } = {},
): Promise<FinanceDocumentUploadResult> {
  const form = new FormData()
  form.append('file', file)
  form.append('company', meta.company || '')
  form.append('period', meta.period || '')
  form.append('doc_type', meta.docType || 'annual_report')
  const { data } = await api.post<FinanceDocumentUploadResult>('/finance/documents', form, {
    // 解析 200 页年报可能超过默认 60s；上传单独放宽。
    timeout: 300000,
  })
  return data
}

export async function sendFinanceChat(
  query: string,
  conversationId?: string,
  maxSteps = 10,
  searchMode: 'sections' | 'blocks' = 'blocks',
  allowWeb = false,
): Promise<FinanceChatResponse> {
  const { data } = await api.post<FinanceChatResponse>('/finance/chat', {
    query,
    conversation_id: conversationId,
    max_steps: maxSteps,
    search_mode: searchMode,
    // 产品默认自动选路；UI 不暴露固定窗 RAG 或 Agent/RAG 开关。
    strategy: 'auto',
    // 联网是兜底：只有用户显式勾选、且本地语料未覆盖时才走综述。
    allow_web: allowWeb,
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
