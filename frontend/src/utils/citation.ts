import type { EvidenceRef } from '../api/client'

/** `doc#t0013#rows#5` → `t0013` */
export function shortTableId(tableId?: string | null): string {
  if (!tableId) return ''
  const parts = tableId.split('#')
  return parts[parts.length - 1] || tableId
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
