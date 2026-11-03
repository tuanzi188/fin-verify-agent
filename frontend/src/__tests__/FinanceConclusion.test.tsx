import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'
import FinanceConclusion from '../components/FinanceConclusion'
import type { FinanceMessagePayload } from '../utils/financeHistory'

const evidence = {
  evidence_id: 'doc#t0013#rows#5#cols#2',
  doc_id: 'doc',
  doc_title: '美的集团2025年年度报告',
  company: '美的集团',
  period: '2025',
  doc_type: 'annual_report',
  kind: 'table_rows' as const,
  page: 58,
  path: [],
  table_id: 'doc#t0013',
  row_indices: [5],
  column_indices: [2],
  unit: '千元',
  footnote: '',
  content: '',
  source_spans: [],
  table_preview: null,
  verified: true,
}

const payload = (overrides: Partial<FinanceMessagePayload> = {}): FinanceMessagePayload => ({
  status: 'answered',
  strategy: 'structured_rag',
  route_reason: '单公司单期间单指标',
  claims: [{ text: '营业收入 299,927,239 千元。', citation_markers: [1], calc_index: null }],
  citations: [{ marker: 1, evidence }],
  calc: [],
  trace: [],
  notes: [],
  ...overrides,
})

describe('FinanceConclusion', () => {
  it('does not repeat status, strategy or counters on the happy path', () => {
    render(<FinanceConclusion finance={payload()} onEvidenceClick={() => {}} />)
    expect(screen.queryByText('已回答')).toBeNull()
    expect(screen.queryByText('结构检索')).toBeNull()
    expect(screen.queryByText(/条要点/)).toBeNull()
    expect(screen.getByText(/299,927,239/)).toBeTruthy()
  })

  it('shows review reasons when verification reports issues', () => {
    render(
      <FinanceConclusion
        finance={payload({
          status: 'needs_review',
          verification: {
            status: 'needs_review',
            checked_numbers: 3,
            issues: [
              {
                claim_index: 0,
                value: '65.71',
                unit: '%',
                reason: '引用或计算记录中缺少同单位等价数值',
              },
            ],
          },
        })}
        onEvidenceClick={() => {}}
      />,
    )
    expect(screen.getByText('需要人工复核')).toBeTruthy()
    // 先说已核验什么，再说哪里没过
    expect(screen.getByText(/已核验 3 项 · 数值未对上/)).toBeTruthy()
    expect(screen.getByText(/第 1 条 · 65\.71%/)).toBeTruthy()
    expect(screen.getByText(/数值未在引用表格中找到对应值/)).toBeTruthy()
  })

  it('shows a short material-missing notice for abstained answers', () => {
    render(
      <FinanceConclusion
        finance={payload({ status: 'abstained', claims: [] })}
        onEvidenceClick={() => {}}
      />,
    )
    expect(screen.getByText('材料不足')).toBeTruthy()
  })

  it('marks uncertainty on the claim instead of a separate block', () => {
    render(
      <FinanceConclusion
        finance={payload({
          claims: [
            {
              text: '占比 65.71%。',
              citation_markers: [1],
              uncertainty: '占比口径需要确认。',
            },
          ],
        })}
        onEvidenceClick={() => {}}
      />,
    )
    expect(screen.getByText('待复核')).toBeTruthy()
    expect(screen.queryByText('不确定性')).toBeNull()
  })
})
