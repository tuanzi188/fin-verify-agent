import React from 'react'
import { Popover, Space, Tag, Typography } from 'antd'
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExclamationCircleOutlined,
  FileTextOutlined,
  TableOutlined,
} from '@ant-design/icons'
import type { CalcRecord, EvidenceRef, FinanceCitation } from '../api/client'
import type { FinanceMessagePayload } from '../hooks/useChat'
import { citationDescriptor, shortTableId } from '../utils/citation'
import { normalizeStatus, STATUS_META, type TurnStatus } from '../utils/status'
import CalcCard from './CalcCard'

const { Text, Paragraph } = Typography

const STATUS_ICON: Record<TurnStatus, React.ReactNode> = {
  answered: <CheckCircleOutlined />,
  abstained: <ExclamationCircleOutlined />,
  clarified: <ExclamationCircleOutlined />,
  needs_review: <ExclamationCircleOutlined />,
  error: <CloseCircleOutlined />,
}

const STATUS_SURFACE: Record<TurnStatus, { bg: string; color: string }> = {
  answered: { bg: 'var(--color-success-bg)', color: 'var(--color-success)' },
  abstained: { bg: 'var(--color-warning-bg)', color: 'var(--color-warning)' },
  clarified: { bg: 'var(--color-info-bg)', color: 'var(--color-primary)' },
  needs_review: { bg: 'var(--color-review-bg)', color: 'var(--color-review)' },
  error: { bg: 'var(--color-error-bg)', color: 'var(--color-error)' },
}

const CitationPill: React.FC<{
  citation: FinanceCitation
  onClick?: () => void
}> = ({ citation, onClick }) => {
  const evidence = citation.evidence
  const descriptor = citationDescriptor(evidence)
  const content = (
    <div style={{ maxWidth: 320 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
        <FileTextOutlined style={{ color: 'var(--color-primary)' }} />
        <Text strong style={{ fontSize: 13 }}>
          {evidence.doc_title || evidence.doc_id}
        </Text>
      </div>
      <Space wrap size={[4, 4]} style={{ marginBottom: 8 }}>
        {evidence.company && <Tag style={{ margin: 0 }}>{evidence.company}</Tag>}
        {evidence.period && <Tag style={{ margin: 0 }}>{evidence.period}</Tag>}
        {evidence.page && <Tag color="blue" style={{ margin: 0 }}>P.{evidence.page}</Tag>}
        {evidence.table_id && (
          <Tag color="cyan" style={{ margin: 0 }}>
            {shortTableId(evidence.table_id)}
          </Tag>
        )}
        {evidence.unit && <Tag color="gold" style={{ margin: 0 }}>{evidence.unit}</Tag>}
      </Space>
      <div
        style={{
          fontSize: 12,
          lineHeight: 1.7,
          color: 'var(--color-text-secondary)',
          whiteSpace: 'pre-wrap',
          maxHeight: 180,
          overflowY: 'auto',
          background: 'var(--color-bg)',
          border: '1px solid var(--color-border)',
          borderRadius: 6,
          padding: '8px 10px',
        }}
      >
        {evidence.content || '正文尚未加载：点击引用，在右侧证据检查器里按锚点回查原文与表格。'}
      </div>
      <div style={{ marginTop: 6, fontSize: 11, color: 'var(--color-text-tertiary)' }}>
        点击打开证据检查器，定位表格行列与计算记录
      </div>
    </div>
  )

  return (
    <Popover content={content} trigger="hover" placement="top" mouseEnterDelay={0.1}>
      <span
        role="button"
        tabIndex={0}
        onClick={(event) => {
          event.preventDefault()
          event.stopPropagation()
          onClick?.()
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault()
            onClick?.()
          }
        }}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 3,
          padding: '1px 6px',
          height: 20,
          fontSize: 11,
          borderRadius: 10,
          background: 'var(--color-primary-light)',
          color: 'var(--color-primary)',
          fontWeight: 600,
          margin: '0 3px',
          verticalAlign: 'text-top',
          cursor: 'pointer',
          border: '1px solid var(--color-primary-border)',
          userSelect: 'none',
        }}
      >
        {evidence.table_id ? <TableOutlined style={{ fontSize: 10 }} /> : <FileTextOutlined style={{ fontSize: 10 }} />}
        <span style={{ fontWeight: 700 }}>[{citation.marker}]</span>
        <span
          title={descriptor}
          style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        >
          {descriptor}
        </span>
      </span>
    </Popover>
  )
}

interface FinanceConclusionProps {
  finance: FinanceMessagePayload
  /** 证据可核验性会把 answered 提升为 needs_review，由工作台统一判定后传入。 */
  statusOverride?: TurnStatus
  onEvidenceClick: (evidence: EvidenceRef | null, calc?: CalcRecord | null) => void
}

const FinanceConclusion: React.FC<FinanceConclusionProps> = ({
  finance,
  statusOverride,
  onEvidenceClick,
}) => {
  const statusKey = statusOverride || normalizeStatus(finance.status)
  const status = STATUS_META[statusKey]
  const surface = STATUS_SURFACE[statusKey]
  const citationByMarker = new Map(finance.citations.map((item) => [item.marker, item]))
  const claims = finance.claims || []
  const uncertainties = claims.filter((claim) => claim.uncertainty)

  const uniqueCitationsFor = (markers: number[]): FinanceCitation[] => {
    const seen = new Set<string>()
    const rows: FinanceCitation[] = []
    for (const marker of markers || []) {
      const citation = citationByMarker.get(marker)
      if (!citation) continue
      const key = citationDescriptor(citation.evidence)
      if (seen.has(key)) continue
      seen.add(key)
      rows.push(citation)
    }
    return rows
  }

  return (
    <div style={{ marginBottom: 12 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '8px 10px',
          borderRadius: 'var(--radius-md)',
          background: surface.bg,
          border: 'var(--border-width) solid var(--color-border)',
          marginBottom: 10,
        }}
      >
        <span style={{ color: surface.color }}>{STATUS_ICON[statusKey]}</span>
        <Text strong style={{ fontSize: 12 }}>{status.label}</Text>
        <Tag
          color={finance.strategy === 'structured_rag' ? 'cyan' : 'blue'}
          style={{ margin: 0, fontSize: 10 }}
          title={finance.route_reason || undefined}
        >
          {finance.strategy === 'structured_rag' ? '结构检索' : '多步查证'}
        </Tag>
        <Text type="secondary" style={{ fontSize: 11 }}>
          {claims.length > 0 ? `${claims.length} 条要点` : '没有结构化要点'}
          {finance.citations.length > 0 ? ` · ${finance.citations.length} 条证据` : ''}
          {finance.calc.length > 0 ? ` · ${finance.calc.length} 次计算` : ''}
        </Text>
      </div>

      {claims.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {claims.map((claim, index) => (
            <div
              key={`${index}-${claim.text.slice(0, 12)}`}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 8,
                fontSize: 13,
                lineHeight: 1.7,
                color: 'var(--color-text)',
              }}
            >
              <span
                style={{
                  flexShrink: 0,
                  width: 18,
                  height: 18,
                  borderRadius: '50%',
                  background: 'var(--color-bg)',
                  border: '1px solid var(--color-border)',
                  color: 'var(--color-text-tertiary)',
                  fontSize: 10,
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  marginTop: 2,
                  fontVariantNumeric: 'tabular-nums',
                }}
              >
                {index + 1}
              </span>
              <div style={{ minWidth: 0 }}>
                <span>{claim.text}</span>
                {uniqueCitationsFor(claim.citation_markers || []).map((citation) => (
                  <CitationPill
                    key={`${index}-${citation.marker}`}
                    citation={citation}
                    onClick={() => onEvidenceClick(citation.evidence)}
                  />
                ))}
                {claim.calc_index !== undefined && claim.calc_index !== null && finance.calc[claim.calc_index] && (
                  <CalcCard
                    calc={finance.calc[claim.calc_index]}
                    index={claim.calc_index}
                    tone="success"
                    compact
                    hint="点击核对输入"
                    notePrefix="口径："
                    onClick={() =>
                      onEvidenceClick(
                        finance.citations[0]?.evidence || null,
                        finance.calc[claim.calc_index as number],
                      )
                    }
                  />
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {claims.length === 0 && finance.calc.length > 0 && (
        <div>
          {finance.calc.map((calc, index) => (
              <CalcCard
                key={index}
                calc={calc}
                index={index}
                tone="success"
                compact
                hint="点击核对输入"
                notePrefix="口径："
                onClick={() => onEvidenceClick(finance.citations[0]?.evidence || null, calc)}
              />
          ))}
        </div>
      )}

      {uncertainties.length > 0 && (
        <div
          style={{
            marginTop: 10,
            padding: '8px 10px',
            borderRadius: 'var(--radius-md)',
            background: 'var(--color-warning-soft)',
            border: '1px solid var(--color-warning-border)',
          }}
        >
          <Text strong style={{ fontSize: 11, color: 'var(--color-warning-text)' }}>不确定性</Text>
          <Paragraph style={{ margin: '4px 0 0', fontSize: 12, color: 'var(--color-text-secondary)', lineHeight: 1.6 }}>
            {uncertainties.map((claim) => claim.uncertainty).filter(Boolean).join('；')}
          </Paragraph>
        </div>
      )}

      {finance.notes && finance.notes.length > 0 && (
        <div style={{ marginTop: 8, fontSize: 10, color: 'var(--color-text-tertiary)', lineHeight: 1.6 }}>
          {finance.notes.slice(0, 4).join(' · ')}
        </div>
      )}
    </div>
  )
}

export default FinanceConclusion
