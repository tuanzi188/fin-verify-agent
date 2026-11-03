import type { EvidenceRef } from '../api/client'

/** `doc#t0013#rows#5` → `t0013` */
export function shortTableId(tableId?: string | null): string {
  if (!tableId) return ''
  const parts = tableId.split('#')
  return parts[parts.length - 1] || tableId
}

const DOC_TYPE_LABELS: Record<string, string> = {
  annual_report: '年报',
  quarterly_report: '季报',
  prospectus: '募集说明书',
  audit_report: '审计报告',
  fund_report: '基金报告',
  announcement: '公告',
}

/**
 * 文档类型 → 用户能读的标签。
 * 不在这里写死"年报"：加债券 / 基金 / 公告时只补一行映射，模板不用动。
 */
export function docTypeLabel(docType?: string | null): string {
  if (!docType) return ''
  return DOC_TYPE_LABELS[docType] || docType
}

export type LocatorPrecision = 'cell' | 'rows' | 'paragraph' | 'document'

/**
 * 这条引用能定位到多细：单元格 / 行组 / 段落 / 文档。
 * “验证范围”里最实在的一格——数字能不能逐格回查，一眼可见。
 */
export function locatorPrecision(evidence: EvidenceRef): LocatorPrecision {
  if (evidence.kind === 'table_cell') return 'cell'
  if (evidence.table_id) return 'rows'
  if (evidence.kind === 'paragraph') return 'paragraph'
  return 'document'
}

export function locatorPrecisionLabel(precision: LocatorPrecision): string {
  switch (precision) {
    case 'cell':
      return '精确到单元格'
    case 'rows':
      return '精确到行组'
    case 'paragraph':
      return '仅段落'
    default:
      return '文档级'
  }
}

export function shortDocTitle(evidence: EvidenceRef, limit = 14): string {
  const title = evidence.doc_title || evidence.doc_id
  if (!title) return '未命名文档'
  return title.length > limit ? `${title.slice(0, limit)}…` : title
}

/** 行标签：优先行名；否则用行号区间。 */
export function rowLabel(evidence: EvidenceRef): string {
  const names = (evidence.row_name || '')
    .split('、')
    .map((name) => name.trim())
    .filter(Boolean)
  if (names.length === 1 && names[0].length <= 10) return names[0]
  if (!evidence.row_indices?.length) return ''
  const first = evidence.row_indices[0]
  const last = evidence.row_indices[evidence.row_indices.length - 1]
  return first === last ? `行${first}` : `行${first}-${last}`
}

export function columnLabel(evidence: EvidenceRef): string {
  if (evidence.column) return evidence.column
  if (!evidence.column_indices?.length) return ''
  return evidence.column_indices.map((index) => `列${index}`).join('、')
}

/**
 * 引用胶囊的统一描述：文档 · 页码 · 表格 · 目标行列 · 单位。
 * 每个引用都尽量给出同一组字段，避免有的胶囊只有编号、有的只有页码。
 */
export function citationParts(evidence: EvidenceRef, options: { includeDoc?: boolean } = {}): string[] {
  const parts: string[] = []
  if (options.includeDoc !== false) parts.push(shortDocTitle(evidence))
  if (evidence.page) parts.push(`P.${evidence.page}`)
  if (evidence.table_id) parts.push(shortTableId(evidence.table_id))
  const rows = rowLabel(evidence)
  if (rows) parts.push(rows)
  const columns = columnLabel(evidence)
  if (columns) parts.push(columns)
  return parts
}

export function citationDescriptor(evidence: EvidenceRef, options: { includeDoc?: boolean } = {}): string {
  const parts = citationParts(evidence, options)
  return parts.join(' · ') || evidence.evidence_id
}

/**
 * 默认态引用胶囊只回答“哪份文档、哪一页”：
 * `美的集团 2025 年报 · P.20`。行列与表名留在 hover 里，避免一条结论挂三行胶囊。
 */
export function citationShortDescriptor(evidence: EvidenceRef): string {
  const parts: string[] = []
  const company = evidence.company || shortDocTitle(evidence, 10)
  if (company) parts.push(company)
  if (evidence.period) parts.push(evidence.period)
  const type = docTypeLabel(evidence.doc_type)
  if (type) parts.push(type)
  else if (evidence.doc_title) parts.push(shortDocTitle(evidence, 12))
  if (evidence.page) parts.push(`· P.${evidence.page}`)
  return parts.join(' ') || evidence.evidence_id
}
