import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import React from 'react'
import MessageItem from '../components/MessageItem'

const financePayload = {
  status: 'answered' as const,
  strategy: 'agent',
  route_reason: '多步查证',
  claims: [{ text: '智能家居收入占比 65.71%', citation_markers: [1], calc_index: 0 }],
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
      },
    },
  ],
  calc: [
    {
      formula: 'a / b * 100',
      inputs: { a: '299927239', b: '456451731' },
      result: '65.7084',
      unit: '%',
    },
  ],
  trace: [],
  notes: [],
}

describe('MessageItem', () => {
  it('renders markdown content for legacy turns', () => {
    render(<MessageItem content="**Bold** text" />)
    expect(screen.getByText('Bold')).toBeTruthy()
  })

  it('renders claims, citation descriptor and calc from the finance payload', () => {
    render(<MessageItem plain content="完整回答" finance={financePayload} />)
    // 结论要点里的数字被单独包裹（等宽强调），整行文本仍完整
    const claimLine = screen.getByText(/智能家居收入占比/)
    expect(claimLine.textContent).toContain('智能家居收入占比 65.71%')
    expect(screen.getByText('65.71%').className).toBe('claim-number')
    // 默认胶囊只给“文档 + 页码”，行列细节留在 hover
    expect(screen.getByText(/美的集团 2025 年报 · P\.58/)).toBeTruthy()
    // 计算默认收起，summary 先给结果；展开后才需要看公式
    const summary = screen.getByText(/计算过程 · 65\.7084/)
    expect(summary.closest('details')?.open).toBe(false)
  })

  it('keeps the default answer view free of strategy, counters and debug notes', () => {
    render(
      <MessageItem
        plain
        content="完整回答"
        finance={{ ...financePayload, notes: ['structured_rag:top_k=6'] }}
      />,
    )
    expect(screen.queryByText('结构检索')).toBeNull()
    expect(screen.queryByText(/条要点/)).toBeNull()
    expect(screen.queryByText(/top_k/)).toBeNull()
  })

  it('calls onCopy when copy button clicked', () => {
    const onCopy = vi.fn()
    render(<MessageItem content="Test content" onCopy={onCopy} />)
    const copyBtn = screen.getAllByRole('button').find((btn) => btn.querySelector('.anticon-copy'))
    expect(copyBtn).toBeTruthy()
    fireEvent.click(copyBtn!)
    expect(onCopy).toHaveBeenCalledWith('Test content')
  })

  it('collapses the user-facing trace by default and expands on click', () => {
    render(
      <MessageItem
        content="Answer"
        trace={[
          { step: 1, tool: 'search_blocks', ok: true, result_count: 3, latency_ms: 42 },
          { step: 2, tool: '__final__', ok: true },
        ]}
      />,
    )
    expect(screen.getByText('查证过程')).toBeTruthy()
    expect(screen.queryByText('检索结构块')).toBeNull()

    fireEvent.click(screen.getByText('查证过程'))

    expect(screen.getByText('检索结构块')).toBeTruthy()
    expect(screen.getByText('3 条结果')).toBeTruthy()
    // 延迟、工具名等实验数据不进入普通查证页面
    expect(screen.queryByText('42 ms')).toBeNull()
  })

  it('plain mode does not render an avatar or bubble chrome', () => {
    render(<MessageItem plain content="毛利率 65.71%。" />)
    expect(screen.getByText('毛利率 65.71%。')).toBeTruthy()
    expect(screen.queryByText('K')).toBeNull()
    expect(screen.queryByText('U')).toBeNull()
  })
})
