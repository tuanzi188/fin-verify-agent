import { describe, it, expect } from 'vitest'
import { citationDescriptor } from '../utils/citation'
import { normalizeEvidence } from '../utils/financeHistory'

const base = {
  evidence_id: 'doc#t0013#rows#5#cols#2',
  doc_id: 'doc',
  doc_title: '美的集团2025年年度报告',
  kind: 'table_rows',
  page: 58,
  table_id: 'doc#t0013',
  row_indices: [5, 6],
  column_indices: [2],
  unit: '千元',
  verified: true,
}

describe('citationDescriptor', () => {
  it('always includes doc, page, table and target rows/columns', () => {
    const evidence = normalizeEvidence(base)!
    const text = citationDescriptor(evidence)
    expect(text).toContain('P.58')
    expect(text).toContain('t0013')
    expect(text).toContain('行5-6')
    expect(text).toContain('列2')
  })

  it('prefers a short row name over row indices', () => {
    const evidence = normalizeEvidence({
      ...base,
      row_name: '营业收入',
      row_indices: [5],
      column_indices: [],
    })!
    expect(citationDescriptor(evidence)).toContain('营业收入')
  })
})
