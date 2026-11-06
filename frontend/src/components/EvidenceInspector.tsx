import React, { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Drawer, Empty, Space, Spin, Tabs, Tag, Tooltip, Typography, message } from 'antd'
import {
  ApartmentOutlined,
  CheckOutlined,
  CloseOutlined,
  CopyOutlined,
  FileTextOutlined,
  LinkOutlined,
} from '@ant-design/icons'
import {
  getFinanceEvidence,
  type AgentTraceStep,
  type CalcRecord,
  type EvidenceRef,
  type FinanceCitation,
} from '../api/client'
import { citationDescriptor } from '../utils/citation'
import { traceStepResult, traceStepTarget, traceToolLabel } from '../utils/trace'
import CalcCard from './CalcCard'

const { Text, Title, Paragraph } = Typography

/** 当前轮与语料的版本关系。 */
export type CorpusStatus = 'current' | 'changed' | 'unknown'

/** 按 evidence_id 缓存回查结果：切换轮次再点回来不重复请求。 */
const evidenceCache = new Map<string, EvidenceRef>()

type HydrationState = 'idle' | 'loading' | 'not_found' | 'error'

/** 历史载荷只存坐标，正文与表格预览需要按需回查。 */
function needsHydration(evidence: EvidenceRef): boolean {
  const tableKind = evidence.kind === 'table_rows' || evidence.kind === 'table_cell'
  return !evidence.content || (tableKind && !evidence.table_preview)
}

interface EvidenceInspectorProps {
  evidence: EvidenceRef | null
  calc?: CalcRecord | null
  calcRecords?: CalcRecord[]
  trace?: AgentTraceStep[]
  /** 本轮结论引用的证据集；没有选中单条时默认展示这个列表。 */
  evidenceList?: FinanceCitation[]
  onSelectEvidence?: (evidence: EvidenceRef) => void
  /** 当前轮落库时的语料版本与现在是否一致。 */
  corpusStatus?: CorpusStatus
  /**
   * 空态的原因，决定给用户看哪句解释：
   * idle=还没提问；legacy=旧会话没保存证据；no_citation=这一轮确实没有可回查引用；
   * concept=概念问答，本来就不引用本地材料。
   */
  emptyReason?: 'idle' | 'legacy' | 'no_citation' | 'concept'
  /** 历史证据回查 404：父组件据此把该轮标成“需人工复核”。 */
  onVerificationIssue?: (evidenceId: string) => void
  open: boolean
  inline?: boolean
  mobile?: boolean
  onClose: () => void
  onSelectCalc?: (calc: CalcRecord) => void
}

const shortId = (value?: string | null): string => {
  if (!value) return '—'
  const parts = value.split('#')
  return parts[parts.length - 1] || value
}

const STATEMENT_SCOPE_LABELS: Record<string, string> = {
  consolidated: '合并口径',
  parent: '母公司口径',
}

const STATEMENT_TYPE_LABELS: Record<string, string> = {
  balance_sheet: '资产负债表',
  income_statement: '利润表',
  cash_flow: '现金流量表',
  equity_change: '所有者权益变动表',
  main_indicators: '主要会计数据',
}

const EvidenceText: React.FC<{ evidence: EvidenceRef }> = ({ evidence }) => {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(evidence.quote || evidence.content || '')
      setCopied(true)
      message.success(evidence.quote ? '原文摘录已复制' : '证据正文已复制')
      setTimeout(() => setCopied(false), 2000)
    } catch {
      message.error('复制失败')
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div>
        <Text type="secondary" style={{ fontSize: 11 }}>来源文档</Text>
        <Title level={5} style={{ margin: '3px 0 0', wordBreak: 'break-all', fontSize: 14 }}>
          {evidence.doc_title || evidence.doc_id}
        </Title>
        <Space wrap size={[4, 4]} style={{ marginTop: 6 }}>
          {evidence.company && <Tag style={{ margin: 0 }}>{evidence.company}</Tag>}
          {evidence.period && <Tag style={{ margin: 0 }}>{evidence.period}</Tag>}
          {evidence.doc_type && <Tag style={{ margin: 0 }}>{evidence.doc_type}</Tag>}
          {evidence.statement_scope && (
            <Tag style={{ margin: 0 }}>
              {STATEMENT_SCOPE_LABELS[evidence.statement_scope] || evidence.statement_scope}
            </Tag>
          )}
          {evidence.statement_type && (
            <Tag style={{ margin: 0 }}>
              {STATEMENT_TYPE_LABELS[evidence.statement_type] || evidence.statement_type}
            </Tag>
          )}
          {evidence.page && <Tag color="blue" style={{ margin: 0 }}>PDF P.{evidence.page}</Tag>}
          {evidence.unit && <Tag color="gold" style={{ margin: 0 }}>单位 {evidence.unit}</Tag>}
          <Tag color={evidence.verified ? 'green' : 'default'} style={{ margin: 0 }}>
            {evidence.verified ? '表格坐标可回查' : '段落证据'}
          </Tag>
          {evidence.quote && (
            <Tag color={evidence.quote_verified ? 'green' : 'orange'} style={{ margin: 0 }}>
              {evidence.quote_verified ? '原文可逐字回查' : '原文未校验'}
            </Tag>
          )}
          {evidence.quote_ambiguous && (
            <Tag color="orange" style={{ margin: 0 }}>
              同文件多处出现
            </Tag>
          )}
        </Space>
      </div>

      {evidence.path.length > 0 && (
        <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', lineHeight: 1.7 }}>
          章节路径：{evidence.path.join(' > ')}
        </div>
      )}

      <div
        style={{
          background: 'var(--color-bg)',
          border: '1px solid var(--color-border)',
          borderRadius: 'var(--radius-md)',
          padding: '10px 12px',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
          <Text type="secondary" style={{ fontSize: 11 }}>证据原文</Text>
          <Button
            type="text"
            size="small"
            icon={copied ? <CheckOutlined style={{ color: 'var(--color-success)' }} /> : <CopyOutlined />}
            onClick={copy}
            style={{ fontSize: 11, height: 22, padding: '0 6px' }}
          >
            {copied ? '已复制' : '复制'}
          </Button>
        </div>
        {evidence.quote && (
          <div
            style={{
              marginBottom: 10,
              padding: '8px 10px',
              background: 'var(--color-primary-bg, rgba(22,119,255,0.06))',
              borderLeft: '3px solid var(--color-primary)',
              borderRadius: 'var(--radius-sm)',
            }}
          >
            <Text type="secondary" style={{ fontSize: 10 }}>原文摘录（逐字）</Text>
            <Paragraph
              style={{
                marginBottom: 0,
                marginTop: 4,
                fontSize: 13,
                lineHeight: 1.75,
                whiteSpace: 'pre-wrap',
                color: 'var(--color-text)',
              }}
            >
              {evidence.quote}
            </Paragraph>
          </div>
        )}
        <Paragraph
          style={{
            marginBottom: 0,
            fontSize: 13,
            lineHeight: 1.75,
            whiteSpace: 'pre-wrap',
            color: 'var(--color-text)',
            maxHeight: '48vh',
            overflowY: 'auto',
          }}
        >
          {evidence.content || '该证据没有可展示的正文。'}
        </Paragraph>
      </div>

      {evidence.note && (
        <Text type="secondary" style={{ fontSize: 11 }}>{evidence.note}</Text>
      )}
      {evidence.source_url && /^https?:\/\//i.test(evidence.source_url) && (
        <div style={{ marginTop: 8 }}>
          <a href={`${evidence.source_url.split('#')[0]}${evidence.page ? `#page=${evidence.page}` : ''}`} target="_blank" rel="noopener noreferrer">
            查看原始披露文件{evidence.page ? `（PDF 第 ${evidence.page} 页）` : ''}
          </a>
        </div>
      )}
    </div>
  )
}

const EvidenceTable: React.FC<{ evidence: EvidenceRef }> = ({ evidence }) => {
  const table = evidence.table_preview
  if (!table) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={
          evidence.table_id
            ? '表格预览尚未加载。历史引用会自动回查；如果回查失败，坐标仍保留在上面的证据清单里。'
            : '该证据没有表格坐标；如果是段落证据，请查看「原文」。'
        }
      />
    )
  }

  const highlightRows = new Set(evidence.row_indices)
  const highlightColumns = new Set(evidence.column_indices)
  const cellsByRow = new Map<number, Map<number, string | null | undefined>>()
  for (const row of table.rows) {
    const map = new Map<number, string | null | undefined>()
    for (const cell of row.cells) map.set(cell.column_index, cell.value)
    cellsByRow.set(row.row, map)
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div>
        <Text strong style={{ fontSize: 13 }} title={table.caption}>
          {table.caption && table.caption.length > 56
            ? `${table.caption.slice(0, 56)}…`
            : table.caption || '未命名表格'}
        </Text>
        <div style={{ marginTop: 6 }}>
          <Space wrap size={[4, 4]}>
            <Tag color="cyan" style={{ margin: 0 }}>{shortId(table.table_id)}</Tag>
            {table.page && <Tag color="blue" style={{ margin: 0 }}>P.{table.page}{table.page_end && table.page_end !== table.page ? `–${table.page_end}` : ''}</Tag>}
            <Tag color="gold" style={{ margin: 0 }}>单位 {table.unit || '未标注'}</Tag>
            {table.unit_source && (
              <Tag style={{ margin: 0 }}>
                {table.unit_source === 'document' ? '文档级单位' : '表格单位'}
              </Tag>
            )}
            <Tag style={{ margin: 0 }}>{table.total_rows} 行</Tag>
          </Space>
        </div>
      </div>

      <div
        style={{
          overflow: 'auto',
          border: '1px solid var(--color-border)',
          borderRadius: 'var(--radius-md)',
          maxHeight: '52vh',
        }}
      >
        <table style={{ borderCollapse: 'collapse', fontSize: 12, minWidth: '100%' }}>
          <thead>
            <tr>
              {table.column_indices.map((columnIndex) => {
                const name =
                  table.display_columns?.[columnIndex] ??
                  table.columns[columnIndex] ??
                  String(columnIndex)
                const isHighlight = highlightColumns.has(columnIndex)
                return (
                  <th
                    key={columnIndex}
                    style={{
                      position: 'sticky',
                      top: 0,
                      left: columnIndex === 0 ? 0 : undefined,
                      zIndex: columnIndex === 0 ? 3 : 2,
                      background: isHighlight ? 'var(--color-highlight-header)' : 'var(--color-bg)',
                      border: '1px solid var(--color-border)',
                      padding: '6px 8px',
                      textAlign: columnIndex === 0 ? 'left' : 'right',
                      whiteSpace: 'nowrap',
                      color: isHighlight ? 'var(--color-primary)' : 'var(--color-text)',
                      fontWeight: 600,
                    }}
                  >
                    {name}
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row) => {
              const rowHighlight = highlightRows.has(row.row)
              return (
                <tr
                  key={row.row}
                  style={{ background: rowHighlight ? 'var(--color-highlight-soft)' : undefined }}
                >
                  {table.column_indices.map((columnIndex) => {
                    const value = cellsByRow.get(row.row)?.get(columnIndex)
                    const columnHighlight = highlightColumns.has(columnIndex)
                    const isTargetCell =
                      rowHighlight && columnHighlight && evidence.kind === 'table_cell'
                    return (
                      <td
                        key={columnIndex}
                        style={{
                          border: '1px solid var(--color-border)',
                          padding: '5px 8px',
                          textAlign: columnIndex === 0 ? 'left' : 'right',
                          whiteSpace: 'nowrap',
                          fontVariantNumeric: 'tabular-nums',
                          position: columnIndex === 0 ? 'sticky' : undefined,
                          left: columnIndex === 0 ? 0 : undefined,
                          zIndex: columnIndex === 0 ? 1 : undefined,
                          background: isTargetCell
                            ? 'var(--color-highlight-cell)'
                            : columnHighlight
                              ? 'var(--color-highlight-soft)'
                              : columnIndex === 0
                                ? rowHighlight
                                  ? 'var(--color-highlight-corner)'
                                  : 'var(--color-bg-white)'
                                : undefined,
                          fontWeight: rowHighlight && columnIndex === 0 ? 600 : 400,
                          color: isTargetCell ? 'var(--color-success)' : 'var(--color-text)',
                        }}
                      >
                        {value ?? ''}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', lineHeight: 1.6 }}>
        {evidence.row_indices.length || evidence.column_indices.length ? (
          <>
            高亮行：{evidence.row_indices.length ? evidence.row_indices.map((row) => `行${row}`).join('、') : '无'}
            {' · '}
            高亮列：{evidence.column_indices.length
              ? evidence.column_indices
                  .map((column) => table.display_columns?.[column] ?? table.columns[column] ?? `列${column}`)
                  .join('、')
              : '无'}
          </>
        ) : (
          '该证据是整表预览，没有指定目标行列；点击单元格级引用可高亮具体坐标。'
        )}
      </div>
      {table.footnote && (
        <div
          style={{
            fontSize: 11,
            color: 'var(--color-text-secondary)',
            background: 'var(--color-bg)',
            border: '1px solid var(--color-border)',
            borderRadius: 6,
            padding: '6px 8px',
            lineHeight: 1.6,
          }}
        >
          脚注：{table.footnote}
        </div>
      )}
      {table.row_indices.length < table.total_rows && (
        <Text type="secondary" style={{ fontSize: 11 }}>
          {evidence.row_indices.length
            ? `当前展示目标行及相邻上下文，共 ${table.row_indices.length} / ${table.total_rows} 行。`
            : `当前展示前 ${table.row_indices.length} / ${table.total_rows} 行预览。`}
        </Text>
      )}
    </div>
  )
}

const CalcDetail: React.FC<{
  calc: CalcRecord | null
  calcRecords: CalcRecord[]
  onSelectCalc?: (calc: CalcRecord) => void
}> = ({ calc, calcRecords, onSelectCalc }) => {
  const records = calc ? [calc] : calcRecords
  if (records.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本次回答没有计算记录。" />
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {records.map((item, index) => (
        <CalcCard
          key={`${item.formula}-${index}`}
          calc={item}
          index={index}
          footer={
            !calc && calcRecords.length > 1 && onSelectCalc ? (
              <Button
                size="small"
                type="link"
                style={{ padding: 0, marginTop: 4 }}
                onClick={() => onSelectCalc(item)}
              >
                只看这一条
              </Button>
            ) : undefined
          }
        />
      ))}
    </div>
  )
}

const EvidenceChain: React.FC<{
  evidence: EvidenceRef
  trace: AgentTraceStep[]
}> = ({ evidence, trace }) => {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ fontSize: 12, lineHeight: 2, color: 'var(--color-text)' }}>
        <div><Text type="secondary">证据锚点：</Text><Text code>{evidence.evidence_id}</Text></div>
        <div><Text type="secondary">文档：</Text><Text code>{evidence.doc_id}</Text></div>
        {evidence.section_id && <div><Text type="secondary">章节：</Text><Text code>{evidence.section_id}</Text></div>}
        {evidence.table_id && <div><Text type="secondary">表格：</Text><Text code>{evidence.table_id}</Text></div>}
        {evidence.table_id && (
          <div>
            <Text type="secondary">坐标：</Text>
            行 {evidence.row_indices.join(',') || '—'} · 列 {evidence.column_indices.join(',') || '—'}
          </div>
        )}
        {evidence.cell_text && <div><Text type="secondary">单元格：</Text>{evidence.cell_text}</div>}
      </div>

      {evidence.source_spans.length > 0 && (
        <div>
          <Text strong style={{ fontSize: 12 }}>原文跨度</Text>
          {evidence.source_spans.map((span, index) => (
            <div key={`${span.source_id}-${index}`} style={{ fontSize: 11, color: 'var(--color-text-secondary)', marginTop: 4 }}>
              {span.kind} · {span.source_id} · P.{span.page ?? '—'} · {span.chars ?? '—'} 字符
            </div>
          ))}
        </div>
      )}

      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
          <ApartmentOutlined style={{ color: 'var(--color-primary)' }} />
          <Text strong style={{ fontSize: 12 }}>查证过程</Text>
        </div>
        {trace.filter((step) => step.tool !== '__final__').length === 0 ? (
          <Text type="secondary" style={{ fontSize: 11 }}>本次没有工具调用记录。</Text>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {trace
              .filter((step) => step.tool !== '__final__')
              .map((step) => (
                <div key={`${step.step}-${step.tool}`} style={{ fontSize: 11, lineHeight: 1.6 }}>
                  <Tag color={step.ok ? 'green' : 'red'} style={{ marginRight: 6, fontSize: 10 }}>
                    {step.step}
                  </Tag>
                  <Text style={{ fontSize: 11 }}>{traceToolLabel(step.tool)}</Text>
                  {traceStepTarget(step) && (
                    <span style={{ color: 'var(--color-text-secondary)', marginLeft: 6 }}>
                      {traceStepTarget(step)}
                    </span>
                  )}
                  <span style={{ color: 'var(--color-text-tertiary)', marginLeft: 6 }}>
                    {traceStepResult(step)}
                  </span>
                  {step.table_id && (
                    <div style={{ color: 'var(--color-text-tertiary)', marginLeft: 22 }}>
                      {shortId(step.table_id)}
                      {step.row_indices?.length ? ` · 行 ${step.row_indices.join(',')}` : ''}
                      {step.column_indices?.length ? ` · 列 ${step.column_indices.join(',')}` : ''}
                    </div>
                  )}
                </div>
              ))}
          </div>
        )}
      </div>
    </div>
  )
}

const KIND_LABEL: Record<EvidenceRef['kind'], string> = {
  paragraph: '段落',
  table_rows: '表格行组',
  table_cell: '单元格',
  document: '文档',
}

/** 默认视图：本轮引用到的证据清单，点击进入单条详情。 */
const EvidenceSet: React.FC<{
  citations: FinanceCitation[]
  onSelect?: (evidence: EvidenceRef) => void
}> = ({ citations, onSelect }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
    <Text type="secondary" style={{ fontSize: 11 }}>
      本轮结论引用的证据（{citations.length}）
    </Text>
    {citations.map(({ marker, evidence }) => (
      <button
        key={`${marker}-${evidence.evidence_id}`}
        type="button"
        onClick={() => onSelect?.(evidence)}
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: 8,
          width: '100%',
          textAlign: 'left',
          padding: '8px 10px',
          borderRadius: 'var(--radius-md)',
          border: '1px solid var(--color-border)',
          background: 'var(--color-bg-white)',
          cursor: onSelect ? 'pointer' : 'default',
          transition: 'border var(--transition-fast)',
        }}
      >
        <span
          style={{
            minWidth: 22,
            height: 20,
            padding: '0 4px',
            borderRadius: 10,
            background: 'var(--color-primary-light)',
            color: 'var(--color-primary)',
            fontSize: 11,
            fontWeight: 600,
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
            marginTop: 1,
          }}
        >
          {marker}
        </span>
        <span style={{ flex: 1, minWidth: 0 }}>
          <span
            style={{
              display: 'block',
              fontSize: 12,
              color: 'var(--color-text)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {evidence.doc_title || evidence.doc_id}
          </span>
          <span style={{ display: 'block', fontSize: 11, color: 'var(--color-text-tertiary)', marginTop: 2 }}>
            {KIND_LABEL[evidence.kind]} · {citationDescriptor(evidence, { includeDoc: false })}
            {evidence.unit ? ` · ${evidence.unit}` : ''}
          </span>
        </span>
        {evidence.verified && (
          <Tag color="green" style={{ margin: 0, fontSize: 10, flexShrink: 0 }}>
            结构化
          </Tag>
        )}
      </button>
    ))}
  </div>
)

const EvidenceInspector: React.FC<EvidenceInspectorProps> = ({
  evidence,
  calc = null,
  calcRecords = [],
  trace = [],
  evidenceList = [],
  onSelectEvidence,
  corpusStatus = 'unknown',
  emptyReason = 'idle',
  onVerificationIssue,
  open,
  inline = false,
  mobile = false,
  onClose,
  onSelectCalc,
}) => {
  const [hydration, setHydration] = useState<HydrationState>('idle')
  const [resolved, setResolved] = useState<EvidenceRef | null>(null)
  const [retryToken, setRetryToken] = useState(0)

  // 历史会话只带坐标：按 evidence_id 回查正文与表格预览。
  // 404 → 证据已不在当前语料；其他错误 → 网络/服务问题，可重试。
  useEffect(() => {
    setResolved(null)
    setHydration('idle')
    if (!evidence || !needsHydration(evidence)) return
    const cached = evidenceCache.get(evidence.evidence_id)
    if (cached) {
      setResolved(cached)
      return
    }
    let cancelled = false
    setHydration('loading')
    getFinanceEvidence({ evidenceId: evidence.evidence_id })
      .then((full) => {
        evidenceCache.set(evidence.evidence_id, full)
        if (!cancelled) {
          setResolved(full)
          setHydration('idle')
        }
      })
      .catch((err) => {
        if (cancelled) return
        const status = (err as { response?: { status?: number } })?.response?.status
        if (status === 404) {
          setHydration('not_found')
          onVerificationIssue?.(evidence.evidence_id)
        } else {
          console.error('证据回查失败:', err)
          setHydration('error')
        }
      })
    return () => {
      cancelled = true
    }
  }, [evidence?.evidence_id, retryToken, onVerificationIssue])

  const activeEvidence = resolved ?? evidence

  const initialTab = useMemo(() => {
    if (calc) return 'calc'
    if (activeEvidence?.kind === 'table_rows' || activeEvidence?.kind === 'table_cell') return 'table'
    return 'text'
  }, [calc, activeEvidence])
  const [tab, setTab] = useState(initialTab)

  useEffect(() => {
    setTab(initialTab)
  }, [initialTab, activeEvidence?.evidence_id, calc])

  const body = (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '10px 12px',
          borderBottom: '1px solid var(--color-border)',
          flexShrink: 0,
        }}
      >
        <Space size={6}>
          <FileTextOutlined style={{ color: 'var(--color-primary)' }} />
          <Text strong style={{ fontSize: 13 }}>证据检查器</Text>
          {activeEvidence && (
            <Tag color={activeEvidence.verified ? 'green' : 'default'} style={{ margin: 0, fontSize: 10 }}>
              {activeEvidence.verified ? '结构化' : '段落'}
            </Tag>
          )}
        </Space>
        <Tooltip title="关闭">
          <Button type="text" size="small" icon={<CloseOutlined />} onClick={onClose} aria-label="关闭证据检查器" />
        </Tooltip>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: '10px 12px 16px' }}>
        {corpusStatus === 'changed' && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 10 }}
            message="会话语料版本已变化"
            description="这条结论来自另一版语料。证据按当前语料回查，页码、行列和数字请人工复核。"
          />
        )}
        {hydration === 'loading' && (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              marginBottom: 10,
              fontSize: 12,
              color: 'var(--color-text-secondary)',
            }}
          >
            <Spin size="small" />
            正在按锚点回查证据正文与表格…
          </div>
        )}
        {hydration === 'not_found' && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 10 }}
            message="当前语料中找不到这条证据"
            description={`锚点 ${evidence?.evidence_id || '未知'} 已失效，可能是文档或表格在重新解析后变了。`}
            action={
              <Button size="small" onClick={() => setRetryToken((token) => token + 1)}>
                重试
              </Button>
            }
          />
        )}
        {hydration === 'error' && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 10 }}
            message="证据回查失败"
            description="没有连上证据接口。坐标仍然保留，可以重试或稍后再看。"
            action={
              <Button size="small" onClick={() => setRetryToken((token) => token + 1)}>
                重试
              </Button>
            }
          />
        )}
        {!activeEvidence && !calc ? (
          evidenceList.length > 0 ? (
            <EvidenceSet citations={evidenceList} onSelect={onSelectEvidence} />
          ) : (
            <div style={{ padding: '28px 20px 20px' }}>
              <div
                style={{
                  fontSize: 12,
                  letterSpacing: 1,
                  color: 'var(--color-text-secondary)',
                  marginBottom: 10,
                }}
              >
                {emptyReason === 'legacy'
                  ? '这一轮没有保存证据'
                  : emptyReason === 'concept'
                    ? '概念问答不引用本地材料'
                    : emptyReason === 'no_citation'
                      ? '这一轮没有可回查的引用'
                      : '等待引用'}
              </div>
              <div
                style={{
                  fontSize: 13,
                  lineHeight: 1.8,
                  color: 'var(--color-text-secondary)',
                  marginBottom: 18,
                }}
              >
                {emptyReason === 'legacy'
                  ? '这条记录来自旧版本会话，只保存了答案文字，引用坐标和表格预览没有落库。重新提问一次就能看到完整证据。'
                  : emptyReason === 'concept'
                    ? '这一轮是金融概念说明，属于通用知识，不引用本地财报材料，也没有可回查的单元格。'
                    : emptyReason === 'no_citation'
                      ? '结论没有附带引用，可能是资料不足、问题需要澄清，或这一轮生成失败。'
                      : '点开结论里的引用胶囊，这里会显示它确实指向哪一格。'}
              </div>
              {[
                ['原文', '章节路径、页码与上下文'],
                ['表格', '目标行列高亮，单位与脚注'],
                ['计算', '公式、输入与逐项复算'],
                ['引用链', '这条结论引了哪几处证据'],
              ].map(([title, desc]) => (
                <div
                  key={title}
                  style={{
                    display: 'flex',
                    gap: 10,
                    padding: '10px 0',
                    borderTop: '1px solid var(--color-border-light)',
                  }}
                >
                  <span
                    style={{
                      width: 3,
                      height: 14,
                      marginTop: 3,
                      borderRadius: 2,
                      background: 'var(--color-primary)',
                      opacity: 0.35,
                      flexShrink: 0,
                    }}
                  />
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-text)' }}>
                      {title}
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginTop: 2 }}>
                      {desc}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )
        ) : (
          <Tabs
            activeKey={tab}
            onChange={setTab}
            size="small"
            items={[
              {
                key: 'text',
                label: '原文',
                children: activeEvidence ? <EvidenceText evidence={activeEvidence} /> : null,
              },
              {
                key: 'table',
                label: '表格',
                children: activeEvidence ? <EvidenceTable evidence={activeEvidence} /> : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="旧版引用没有表格坐标。" />
                ),
              },
              {
                key: 'calc',
                label: '计算',
                children: (
                  <CalcDetail
                    calc={calc}
                    calcRecords={calcRecords}
                    onSelectCalc={onSelectCalc}
                  />
                ),
              },
              {
                key: 'chain',
                label: (
                  <span>
                    <LinkOutlined style={{ marginRight: 4 }} />
                    引用链
                  </span>
                ),
                children: activeEvidence ? (
                  <EvidenceChain evidence={activeEvidence} trace={trace} />
                ) : (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有结构化引用链。" />
                ),
              },
            ]}
          />
        )}
      </div>
    </div>
  )

  if (inline) {
    return (
      <aside
        style={{
          // 空态时不需要占满 380：检查器的宽度应该跟着"有没有证据"走。
          width: evidenceList.length ? 372 : 300,
          minWidth: evidenceList.length ? 372 : 300,
          borderLeft: '1px solid var(--color-border)',
          background: 'var(--color-bg-white)',
          display: 'flex',
          flexDirection: 'column',
          minHeight: 0,
          transition: 'width var(--transition-normal), min-width var(--transition-normal)',
        }}
      >
        {body}
      </aside>
    )
  }

  return (
    <Drawer
      placement={mobile ? 'bottom' : 'right'}
      width={mobile ? undefined : 460}
      height={mobile ? '82vh' : undefined}
      open={open}
      onClose={onClose}
      closable={false}
      styles={{ body: { padding: 0, height: '100%' } }}
    >
      {body}
    </Drawer>
  )
}

export default EvidenceInspector
