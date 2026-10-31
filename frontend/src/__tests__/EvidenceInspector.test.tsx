import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

const mocks = vi.hoisted(() => ({
  getFinanceEvidence: vi.fn(),
}))

vi.mock('../api/client', () => ({
  getFinanceEvidence: mocks.getFinanceEvidence,
}))

import EvidenceInspector from '../components/EvidenceInspector'
import { normalizeEvidence } from '../utils/financeHistory'

const compact = normalizeEvidence({
  evidence_id: 'doc#t0013#rows#5#cols#2',
  doc_id: 'doc',
  doc_title: '美的集团2025年年度报告',
  kind: 'table_rows',
  page: 58,
  table_id: 'doc#t0013',
  row_indices: [5],
  column_indices: [2],
  unit: '千元',
  verified: true,
})!

const full = {
  ...compact,
  content: '表 智能家居收入',
  table_preview: {
    table_id: 'doc#t0013',
    caption: '智能家居收入',
    page: 58,
    unit: '千元',
    columns: ['项目', '2025'],
    display_columns: ['项目', '2025'],
    column_indices: [0, 2],
    rows: [
      {
        row: 5,
        row_name: '智能家居',
        cells: [
          { column: '项目', column_index: 0, value: '智能家居' },
          { column: '2025', column_index: 2, value: '299927239' },
        ],
      },
    ],
    row_indices: [5],
    total_rows: 20,
  },
}

describe('EvidenceInspector history hydration', () => {
  beforeEach(() => {
    mocks.getFinanceEvidence.mockReset()
  })

  it('loads text and table preview by evidence_id', async () => {
    mocks.getFinanceEvidence.mockResolvedValue(full)
    render(<EvidenceInspector inline open evidence={compact} onClose={() => {}} />)

    expect(screen.getByText(/正在按锚点回查/)).toBeTruthy()
    await waitFor(() => {
      expect(mocks.getFinanceEvidence).toHaveBeenCalledWith({ evidenceId: compact.evidence_id })
    })
    expect(await screen.findByText('智能家居收入')).toBeTruthy()
  })

  it('shows a clear state when the anchor no longer exists', async () => {
    // 用独立锚点，绕开上一用例的回查缓存
    const missing = { ...compact, evidence_id: 'doc#t9999#rows#1#cols#1' }
    mocks.getFinanceEvidence.mockRejectedValue({ response: { status: 404 } })
    const onVerificationIssue = vi.fn()
    render(
      <EvidenceInspector
        inline
        open
        evidence={missing}
        onClose={() => {}}
        onVerificationIssue={onVerificationIssue}
      />,
    )

    expect(await screen.findByText('当前语料中找不到这条证据')).toBeTruthy()
    expect(onVerificationIssue).toHaveBeenCalledWith(missing.evidence_id)
  })

  it('explains a corpus version change', async () => {
    mocks.getFinanceEvidence.mockResolvedValue(full)
    render(
      <EvidenceInspector inline open evidence={compact} corpusStatus="changed" onClose={() => {}} />,
    )
    expect(await screen.findByText('会话语料版本已变化')).toBeTruthy()
  })
})
