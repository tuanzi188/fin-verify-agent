import { describe, it, expect } from 'vitest'
import { normalizeEvidence, parseFinancePayload } from '../utils/financeHistory'

const compactPayload = {
  kind: 'finance',
  status: 'answered',
  strategy: 'agent',
  route_reason: '多步查证',
  claims: [{ text: '毛利率 65.71%', citation_markers: [1], calc_index: 0 }],
  citations: [
    {
      marker: 1,
      evidence: {
        evidence_id: 'doc#t0013#rows#5#cols#2',
        doc_id: 'doc',
        doc_title: '美的集团2025年年度报告',
        company: '美的集团',
        period: '2025',
        doc_type: 'annual_report',
        kind: 'table_rows',
        page: 58,
        path: ['第三节', '管理层讨论'],
        table_id: 'doc#t0013',
        table_caption: '智能家居收入',
        row_indices: [5],
        column_indices: [2],
        unit: '千元',
        verified: true,
      },
    },
  ],
  calc: [{ formula: 'a/b*100', inputs: { a: 1 }, result: '65.71', unit: '%' }],
  trace: [{ step: 1, tool: 'search_blocks', ok: true }],
  notes: [],
}

describe('parseFinancePayload', () => {
  it('restores claims, citations and calc from a stored turn', () => {
    const payload = parseFinancePayload(compactPayload)
    expect(payload).toBeDefined()
    expect(payload?.status).toBe('answered')
    expect(payload?.claims[0].citation_markers).toEqual([1])
    expect(payload?.citations[0].marker).toBe(1)
    expect(payload?.citations[0].evidence.table_id).toBe('doc#t0013')
    expect(payload?.citations[0].evidence.row_indices).toEqual([5])
    expect(payload?.calc[0].unit).toBe('%')
  })

  it('fills defaults for fields the compact payload does not store', () => {
    const evidence = parseFinancePayload(compactPayload)?.citations[0].evidence
    expect(evidence?.content).toBe('')
    expect(evidence?.table_preview).toBeNull()
    expect(evidence?.source_spans).toEqual([])
    expect(evidence?.footnote).toBe('')
  })

  it('returns undefined for legacy turns without payload', () => {
    expect(parseFinancePayload(undefined)).toBeUndefined()
    expect(parseFinancePayload(null)).toBeUndefined()
    expect(parseFinancePayload({})).toBeUndefined()
    expect(parseFinancePayload({ kind: 'other' })).toBeUndefined()
  })

  it('drops citations whose evidence anchor is unusable', () => {
    const payload = parseFinancePayload({
      ...compactPayload,
      citations: [{ marker: 1, evidence: { doc_title: '缺坐标' } }],
    })
    expect(payload?.citations).toEqual([])
  })

  it('keeps the corpus fingerprint for re-verification', () => {
    const payload = parseFinancePayload({ ...compactPayload, corpus_fingerprint: 'fp-123' })
    expect(payload?.corpus_fingerprint).toBe('fp-123')
    expect(parseFinancePayload(compactPayload)?.corpus_fingerprint).toBeUndefined()
  })

  it('falls back to error status when status is unknown', () => {
    expect(parseFinancePayload({ ...compactPayload, status: 'weird' })?.status).toBe('error')
  })
})

describe('normalizeEvidence', () => {
  it('keeps unknown kind as document instead of crashing the panel', () => {
    const evidence = normalizeEvidence({
      evidence_id: 'doc#s1',
      doc_id: 'doc',
      kind: 'chart',
    })
    expect(evidence?.kind).toBe('document')
  })
})
