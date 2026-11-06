import type {
  AgentStatus,
  AgentTraceStep,
  CalcRecord,
  Claim,
  EvidenceKind,
  EvidenceRef,
  FinanceCitation,
  MarketBrief,
  RefusalInfo,
  WebSourceRef,
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
  strategy: 'structured_rag' | 'agent' | 'concept' | string
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
  /** 联网综述（未核实）。与 citations 互斥，不进引用集合。 */
  market_brief?: MarketBrief
  /** 拒答分级（六类之一）：只有 abstained / clarified 轮次才有。 */
  refusal?: RefusalInfo
  /** 概念问答：首段定论（正文 answer 里仍保留全文）。 */
  answer_lead?: string
  /** 概念问答实际检索到的公开信息，只做透明展示。 */
  web_sources?: WebSourceRef[]
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

function normalizeMarketBrief(raw: unknown): MarketBrief | undefined {
  const data = asRecord(raw)
  if (!data) return undefined
  const sections = asRecord(data.sections) ?? {}
  return {
    ok: data.ok === true,
    question: asString(data.question),
    summary: asString(data.summary),
    sections: Object.fromEntries(
      Object.entries(sections).map(([key, value]) => [key, asArray<string>(value)]),
    ),
    misreadings: asArray<string>(data.misreadings),
    divergences: asArray<string>(data.divergences),
    gaps: asArray<string>(data.gaps),
    sources: asArray<MarketBrief['sources'][number]>(data.sources),
    skipped_sources: asArray<NonNullable<MarketBrief['skipped_sources']>[number]>(
      data.skipped_sources,
    ),
    ingest_candidates: asArray<NonNullable<MarketBrief['ingest_candidates']>[number]>(
      data.ingest_candidates,
    ),
    raw: asString(data.raw) || undefined,
    as_of: asString(data.as_of),
    disclaimer: asString(data.disclaimer),
    warnings: asArray<string>(data.warnings),
    queries: asArray<string>(data.queries),
    skipped_reason: asString(data.skipped_reason) || null,
    error: asString(data.error) || null,
  }
}

/** 拒答类别缺字段时按缺失处理：宁可不显示标签，也不猜级别。 */
function normalizeRefusal(raw: unknown): RefusalInfo | undefined {
  const data = asRecord(raw)
  if (!data) return undefined
  const code = asString(data.code)
  const label = asString(data.label)
  const level = typeof data.level === 'number' ? data.level : Number(data.level)
  if (!code || !label || !Number.isFinite(level)) return undefined
  return {
    code,
    label,
    level,
    level_label: asString(data.level_label),
    action: asString(data.action),
    action_label: asString(data.action_label),
    recover_by: asString(data.recover_by),
    hint: asString(data.hint),
    detail: asString(data.detail) || undefined,
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
    market_brief: normalizeMarketBrief(data.market_brief),
    refusal: normalizeRefusal(data.refusal),
    answer_lead: asString(data.answer_lead) || undefined,
    web_sources: asArray<WebSourceRef>(data.web_sources),
  }
}
