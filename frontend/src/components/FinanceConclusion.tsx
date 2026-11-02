import React from 'react'
import { Popover, Space, Tag, Typography } from 'antd'
import {
  CloseCircleOutlined,
  ExclamationCircleOutlined,
  FileTextOutlined,
  TableOutlined,
} from '@ant-design/icons'
import type { CalcRecord, EvidenceRef, FinanceCitation } from '../api/client'
import type {
  FinanceMessagePayload,
  FinanceVerificationIssue,
} from '../utils/financeHistory'
import { citationDescriptor, citationShortDescriptor, docTypeLabel, shortTableId } from '../utils/citation'
import { normalizeStatus, type TurnStatus } from '../utils/status'
import CalcCard from './CalcCard'

const { Text } = Typography

/**
 * 默认态只回答三件事：结论是什么、数字从哪来、哪里不确定。
 * 策略、计数、top_k、trace 属于技术详情，不在结论卡里出现。
 */

/** 结论要点里的数字用等宽字体强调：金融结论要先能扫读，再能细读。 */
const CLAIM_NUMBER_RE =
  /(\d[\d,]*(?:\.\d+)?(?:\s*(?:%|％|亿元|万元|千元|元|倍|个百分点))?)/g

const renderClaimText = (text: string): React.ReactNode[] =>
  text.split(CLAIM_NUMBER_RE).map((part, index) =>
    index % 2 === 1 ? (
      <span key={index} className="claim-number">
        {part}
      </span>
    ) : (
      <React.Fragment key={index}>{part}</React.Fragment>
    ),
  )

/** 后端 reason code → 用户能读的一句话；未知 reason 原样显示，不吞信息。 */
const ISSUE_REASON_LABELS: Record<string, string> = {
  引用或计算记录中缺少同单位等价数值: '数值未在引用表格中找到对应值',
  计算公式或输入来源尚未完整验证: '计算输入来源未能完整追溯',
  要点缺少明确引用或计算记录: '缺少引用或计算依据',
  引用编号无法回查: '引用编号无法回查',
  回答未形成可核验要点: '回答没有形成可核验的要点',
}

function issueText(issue: FinanceVerificationIssue): string {
  const head: string[] = []
  if (typeof issue.claim_index === 'number') head.push(`第 ${issue.claim_index + 1} 条`)
  if (issue.value) head.push(`${issue.value}${issue.unit || ''}`)
  const reason = issue.reason || ''
  const text = ISSUE_REASON_LABELS[reason] || reason || '需要人工核对'
  return head.length ? `${head.join(' · ')}：${text}` : text
}

const CitationPill: React.FC<{
  citation: FinanceCitation
  onClick?: () => void
}> = ({ citation, onClick }) => {
  const evidence = citation.evidence
  const label = citationShortDescriptor(evidence)
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
        {docTypeLabel(evidence.doc_type) && (
          <Tag style={{ margin: 0 }}>{docTypeLabel(evidence.doc_type)}</Tag>
        )}
        {evidence.page && <Tag color="blue" style={{ margin: 0 }}>P.{evidence.page}</Tag>}
        {evidence.table_id && (
          <Tag color="cyan" style={{ margin: 0 }}>
            {evidence.table_caption || shortTableId(evidence.table_id)}
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
          title={citationShortDescriptor(evidence)}
          style={{ maxWidth: 190, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        >
          {label}
        </span>
      </span>
    </Popover>
  )
}

/** 不确定标记跟着要点走，不再汇总成底部一块。 */
const ReviewFlag: React.FC<{ reason: string }> = ({ reason }) => (
  <Popover
    content={
      <div style={{ maxWidth: 300, fontSize: 12, lineHeight: 1.7, color: 'var(--color-text-secondary)' }}>
        {reason}
      </div>
    }
    trigger="hover"
    placement="top"
  >
    <span role="button" tabIndex={0} className="claim-flag">
      待复核
    </span>
  </Popover>
)

const REVIEW_COPY: Record<string, { title: string; body: string }> = {
  abstained: { title: '材料不足', body: '本地语料没有覆盖这个问题，或问题超出可查范围。' },
  clarified: { title: '需要补充条件', body: '请补充公司、期间或指标后再查证。' },
  needs_review: { title: '需要人工复核', body: '这条结论的数值、引用或计算来源未能通过自动校验。' },
  error: { title: '这次查证没有完成', body: '请重试，或缩小问题范围。' },
  legacy: { title: '历史结果', body: '这条记录来自旧版本会话，只保存了答案文字。' },
}

/** 正常态不渲染状态条；只有需要用户行动时才出现。 */
const ReviewAlert: React.FC<{
  statusKey: TurnStatus
  verification?: FinanceMessagePayload['verification']
  claims?: FinanceMessagePayload['claims']
}> = ({ statusKey, verification, claims }) => {
  if (statusKey === 'answered') return null
  const copy = REVIEW_COPY[statusKey]
  if (!copy) return null
  const issues = verification?.issues || []
  const fallback = (claims || [])
    .map((claim) => claim.uncertainty)
    .filter((item): item is string => Boolean(item))
  const details = issues.length
    ? issues.map(issueText)
    : statusKey === 'needs_review'
      ? fallback
      : []
  const visible = details.slice(0, 4)
  const tone =
    statusKey === 'error'
      ? { bg: 'var(--color-error-bg)', color: 'var(--color-error)', border: 'var(--color-error)' }
      : statusKey === 'abstained'
        ? { bg: 'var(--color-warning-bg)', color: 'var(--color-warning)', border: 'var(--color-warning)' }
        : { bg: 'var(--color-review-bg)', color: 'var(--color-review)', border: 'var(--color-review)' }
  return (
    <div
      className="review-alert"
      style={{ background: tone.bg, borderColor: `color-mix(in srgb, ${tone.border} 35%, transparent)` }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {statusKey === 'error' ? (
          <CloseCircleOutlined style={{ color: tone.color }} />
        ) : (
          <ExclamationCircleOutlined style={{ color: tone.color }} />
        )}
        <Text strong style={{ fontSize: 12, color: tone.color }}>
          {copy.title}
        </Text>
      </div>
      <div className="review-alert-body">{copy.body}</div>
      {visible.length > 0 && (
        <ul className="review-alert-list">
          {visible.map((item, index) => (
            <li key={index}>{item}</li>
          ))}
          {details.length > visible.length && (
            <li>还有 {details.length - visible.length} 项，详见技术详情。</li>
          )}
        </ul>
      )}
    </div>
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
  const citationByMarker = new Map(finance.citations.map((item) => [item.marker, item]))
  const claims = finance.claims || []

  const uniqueCitationsFor = (markers: number[]): FinanceCitation[] => {
    const seen = new Set<string>()
    const rows: FinanceCitation[] = []
    for (const marker of markers || []) {
      const citation = citationByMarker.get(marker)
      if (!citation) continue
      // 去重按完整坐标：同一文档同一页的两张表不能被短标签合并掉。
      const key = citationDescriptor(citation.evidence)
      if (seen.has(key)) continue
      seen.add(key)
      rows.push(citation)
    }
    return rows
  }

  const calcFor = (claim: FinanceMessagePayload['claims'][number]): CalcRecord | null => {
    if (claim.calc_index === undefined || claim.calc_index === null) return null
    return finance.calc[claim.calc_index] || null
  }

  return (
    <div style={{ marginBottom: 12 }}>
      <ReviewAlert statusKey={statusKey} verification={finance.verification} claims={claims} />

      {claims.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {claims.map((claim, index) => {
            const calc = calcFor(claim)
            return (
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
                  <span>{renderClaimText(claim.text)}</span>
                  {uniqueCitationsFor(claim.citation_markers || []).map((citation) => (
                    <CitationPill
                      key={`${index}-${citation.marker}`}
                      citation={citation}
                      onClick={() => onEvidenceClick(citation.evidence)}
                    />
                  ))}
                  {claim.uncertainty && <ReviewFlag reason={claim.uncertainty} />}

                  {calc && (
                    <details className="claim-calc">
                      <summary>
                        计算过程 · {String(calc.result)}
                        {calc.unit ? ` ${calc.unit}` : ''}
                      </summary>
                      <CalcCard
                        calc={calc}
                        index={claim.calc_index as number}
                        tone="success"
                        compact
                        hint="点击核对输入"
                        notePrefix="口径："
                        onClick={() =>
                          onEvidenceClick(
                            finance.citations.find((citation) =>
                              Object.values(calc.input_sources || {}).some(
                                (source) => source.evidence_id === citation.evidence.evidence_id,
                              ),
                            )?.evidence || null,
                            calc,
                          )
                        }
                      />
                    </details>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}

      {claims.length === 0 && finance.calc.length > 0 && (
        <details className="claim-calc">
          <summary>查看计算过程 · {finance.calc.length} 次</summary>
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
        </details>
      )}
    </div>
  )
}

export default FinanceConclusion
