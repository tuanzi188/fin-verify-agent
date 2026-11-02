import type {
  AgentStatus,
  AgentTraceStep,
  CalcRecord,
  Claim,
  EvidenceKind,
  EvidenceRef,
  FinanceCitation,
} from '../api/client'

/**
 * 会话历史里的金融查证载荷（见 backend/app/services/finance_history.py）。
 *
 * 与 `/finance/chat` 的响应同形，但引用里的 `content` / `table_preview`
 * 不落库，打开旧会话时按 `evidence_id` 回查。
 */
export interface FinanceVerificationIssue {
  claim_index?: number
  reason?: string
  marker?: number
  value?: string
  unit?: string
}

/** 后端确定性复核摘要；只用于「存在问题时显示复核原因」，不参与正常态展示。 */
export interface FinanceVerification {
  scope?: string
  checked_numbers?: number
  issues?: FinanceVerificationIssue[]
  status?: string
}

export interface FinanceMessagePayload {
  status: AgentStatus
  strategy: 'structured_rag' | 'agent' | string
  route_reason: string
  claims: Claim[]
  citations: FinanceCitation[]
  calc: CalcRecord[]
  trace: AgentTraceStep[]
  notes?: string[]
  verification?: FinanceVerification
  resolved_query?: string
  /** 落库时的块索引指纹；和 /finance/stats 对比可判断语料是否变化。 */
  corpus_fingerprint?: string
}

const EVIDENCE_KINDS: EvidenceKind[] = ['paragraph', 'table_rows', 'table_cell', 'document']
const AGENT_STATUSES: AgentStatus[] = ['answered', 'abstained', 'clarified', 'needs_review', 'error']

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : []
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function asNumberArray(value: unknown): number[] {
  return asArray<unknown>(value)
    .map((item) => (typeof item === 'number' ? item : Number(item)))
    .filter((item) => Number.isFinite(item))
}

function normalizeVerification(raw: unknown): FinanceVerification | undefined {
  const data = asRecord(raw)
  if (!data) return undefined
  const issues: FinanceVerificationIssue[] = []
  for (const item of asArray<unknown>(data.issues)) {
    const issue = asRecord(item)
    if (!issue) continue
    issues.push({
      claim_index: typeof issue.claim_index === 'number' ? issue.claim_index : undefined,
      reason: asString(issue.reason) || undefined,
      marker: typeof issue.marker === 'number' ? issue.marker : undefined,
      value: issue.value === undefined || issue.value === null ? undefined : String(issue.value),
      unit: asString(issue.unit) || undefined,
    })
  }
  return {
    scope: asString(data.scope) || undefined,
    checked_numbers: typeof data.checked_numbers === 'number' ? data.checked_numbers : undefined,
    issues,
    status: asString(data.status) || undefined,
  }
}

/** 历史载荷只有坐标，没有正文和表格预览；缺失字段在这里补默认值，避免渲染时 undefined。 */
export function normalizeEvidence(raw: unknown): EvidenceRef | null {
  const data = asRecord(raw)
  if (!data) return null
  const evidenceId = asString(data.evidence_id)
  const docId = asString(data.doc_id)
  if (!evidenceId || !docId) return null

  const kind = EVIDENCE_KINDS.includes(data.kind as EvidenceKind)
    ? (data.kind as EvidenceKind)
    : 'document'

  return {
    evidence_id: evidenceId,
    doc_id: docId,
    doc_title: asString(data.doc_title),
    company: asString(data.company),
    period: asString(data.period),
    doc_type: asString(data.doc_type),
    kind,
    page: typeof data.page === 'number' ? data.page : null,
    section_id: asString(data.section_id) || null,
    path: asArray<string>(data.path).filter((item) => typeof item === 'string'),
    table_id: asString(data.table_id) || null,
    table_caption: asString(data.table_caption) || null,
    row_indices: asNumberArray(data.row_indices),
    column_indices: asNumberArray(data.column_indices),
    row_name: asString(data.row_name) || null,
    column: asString(data.column) || null,
    cell_text: asString(data.cell_text) || null,
    unit: asString(data.unit),
    footnote: asString(data.footnote),
    content: asString(data.content),
    source_spans: asArray<EvidenceRef['source_spans'][number]>(data.source_spans),
    table_preview: (data.table_preview as EvidenceRef['table_preview']) ?? null,
    verified: data.verified === true,
    note: asString(data.note) || null,
    source_url: asString(data.source_url) || null,
  }
}

/**
 * 把 `ConversationTurn.payload` 还原成前端消息载荷。
 * 旧会话（没有 payload）或格式不符时返回 undefined，调用方退回 Markdown。
 */
export function parseFinancePayload(raw: unknown): FinanceMessagePayload | undefined {
  const data = asRecord(raw)
  if (!data || data.kind !== 'finance') return undefined

  const citations: FinanceCitation[] = []
  for (const item of asArray<unknown>(data.citations)) {
    const citation = asRecord(item)
    if (!citation) continue
    const evidence = normalizeEvidence(citation.evidence)
    const marker = typeof citation.marker === 'number' ? citation.marker : Number(citation.marker)
    if (!evidence || !Number.isFinite(marker)) continue
    citations.push({ marker, evidence })
  }

  const status = AGENT_STATUSES.includes(data.status as AgentStatus)
    ? (data.status as AgentStatus)
    : 'error'

  return {
    status,
    strategy: asString(data.strategy, 'agent'),
    route_reason: asString(data.route_reason),
    claims: asArray<Claim>(data.claims),
    citations,
    calc: asArray<CalcRecord>(data.calc),
    trace: asArray<AgentTraceStep>(data.trace),
    notes: asArray<string>(data.notes),
    verification: normalizeVerification(data.verification),
    resolved_query: asString(data.resolved_query) || undefined,
    corpus_fingerprint: asString(data.corpus_fingerprint) || undefined,
  }
}
