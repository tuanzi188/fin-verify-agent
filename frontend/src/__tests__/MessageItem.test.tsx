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

  it('marks concept answers as general knowledge without evidence chrome', () => {
    const conceptPayload = {
      ...financePayload,
      strategy: 'concept' as const,
      route_reason: '金融概念/知识问题，不检索本地材料',
      claims: [],
      citations: [],
      calc: [],
    }
    render(<MessageItem plain content="毛利率 = 毛利 ÷ 营业收入。" finance={conceptPayload} />)
    expect(screen.getByText('通用知识 · 未核实')).toBeTruthy()
    // 概念答案没有引用，但不应被当成异常轮次
    expect(screen.queryByText(/需人工复核/)).toBeNull()
  })

  it('labels concept answers that used web sources', () => {
    const conceptPayload = {
      ...financePayload,
      strategy: 'concept' as const,
      claims: [],
      citations: [],
      calc: [],
      notes: ['concept:web', 'concept:web_used'],
    }
    render(<MessageItem plain content="可转债在转股期内可转为股票。" finance={conceptPayload} />)
    expect(screen.getByText('通用知识 + 公开信息 · 未核实')).toBeTruthy()
  })

  it('labels rate/rule answers as public information, not general knowledge', () => {
    const conceptPayload = {
      ...financePayload,
      strategy: 'concept' as const,
      claims: [],
      citations: [],
      calc: [],
      notes: ['concept:web_fact', 'concept:web_forced', 'concept:web_unused'],
    }
    render(
      <MessageItem
        plain
        content="卖出印花税：成交金额的 0.05%，单边征收。"
        finance={conceptPayload}
      />,
    )
    expect(screen.getByText('公开信息 · 未核实')).toBeTruthy()
  })

  it('renders the lead once and lists retrieved web sources', () => {
    const lead = '商誉是并购时多付的那部分溢价。'
    const conceptPayload = {
      ...financePayload,
      strategy: 'concept' as const,
      claims: [],
      citations: [],
      calc: [],
      notes: ['concept:web', 'concept:web_used'],
      answer_lead: lead,
      web_sources: [
        {
          label: '中国证券报',
          domain: 'cs.com.cn',
          title: '商誉减值风险',
          url: 'https://www.cs.com.cn/a',
          published_at: '2025-11-01',
        },
      ],
    }
    render(
      <MessageItem
        plain
        content={`# 商誉\n\n${lead}\n\n## 细节\n\n后续解释。`}
        finance={conceptPayload}
      />,
    )
    // 定论只出现一次：正文里要把它去掉
    expect(screen.getAllByText(lead)).toHaveLength(1)
    // 来源默认收起，摘要行给数量
    const summary = screen.getByText(/引用来源/)
    expect(summary.closest('details')?.open).toBe(false)
    expect(screen.getByText(/1 篇/)).toBeTruthy()
    expect(screen.getByText('商誉减值风险')).toBeTruthy()
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
    expect(screen.queryByText(/条要点/)).toBeNull()
    // 策略和调试备注只在技术详情里，默认收起
    const details = screen.getByText('技术详情').closest('details')
    expect(details?.open).toBe(false)
    expect(details?.textContent).toContain('多步查证')
    expect(screen.getByText(/top_k=6/)).toBeTruthy()
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

  it('keeps technical details collapsed and shows verification scope when expanded', () => {
    render(
      <MessageItem
        plain
        content="完整回答"
        finance={{
          ...financePayload,
          verification: { checked_numbers: 2, issues: [] },
          notes: ['structured_rag:top_k=6'],
        }}
      />,
    )
    const summary = screen.getByText('技术详情')
    // 默认收起；内容留在 DOM 里，展开不需要额外请求
    expect(summary.closest('details')?.open).toBe(false)
    expect(screen.getByText(/已核验 2 项，未发现不一致/)).toBeTruthy()
    expect(screen.getByText(/行组 1/)).toBeTruthy()
    expect(screen.getByText(/top_k=6/)).toBeTruthy()
  })

  it('plain mode does not render an avatar or bubble chrome', () => {
    render(<MessageItem plain content="毛利率 65.71%。" />)
    expect(screen.getByText('毛利率 65.71%。')).toBeTruthy()
    expect(screen.queryByText('K')).toBeNull()
    expect(screen.queryByText('U')).toBeNull()
  })

  it('renders GFM tables instead of raw pipe text', () => {
    // 财报答案常用定位表；表格是 GFM 扩展，缺 remark-gfm 时整块退化成竖线文本
    render(
      <MessageItem
        plain
        content={'| 项目 | 2025 |\n|---|---|\n| 营业收入 | 456,451,731 |'}
      />,
    )
    const table = document.querySelector('table')
    expect(table).toBeTruthy()
    // 长表会撑破侧栏，表格外层必须是可横向滚动的容器
    expect(table?.parentElement?.style.overflowX).toBe('auto')
    expect(screen.getByText('营业收入')).toBeTruthy()
    expect(screen.queryByText(/\|---\|/)).toBeNull()
  })
})
