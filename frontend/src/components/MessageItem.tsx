import React from 'react'
import { Button, Space, message } from 'antd'
import { CopyOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import type { AgentTraceStep, CalcRecord, EvidenceRef } from '../api/client'
import type { FinanceMessagePayload } from '../hooks/useChat'
import type { TurnStatus } from '../utils/status'
import { locatorPrecision } from '../utils/citation'
import AgentTracePanel from './AgentTracePanel'
import FinanceConclusion from './FinanceConclusion'

interface MessageItemProps {
  content: string
  trace?: AgentTraceStep[]
  finance?: FinanceMessagePayload
  /**
   * 轮次条的最终展示状态。结论卡必须和它对齐——否则语料变化时
   * 顶部提示"需人工复核"、卡片仍是绿色"已回答"。
   */
  statusOverride?: TurnStatus
  /** 工作台模式：去掉外框；保留给非工作台场景兜底。 */
  plain?: boolean
  onEvidenceClick?: (evidence: EvidenceRef | null, calc?: CalcRecord | null) => void
  onCopy?: (text: string) => void
}

/** 自定义代码块：带语言标签与一键复制。 */
const CodeBlock: React.FC<{
  className?: string
  children?: React.ReactNode
}> = ({ className, children, ...props }) => {
  const [copied, setCopied] = React.useState(false)
  const match = /language-(\w+)/.exec(className || '')
  const language = match ? match[1] : ''
  const rawText = String(children)
  const codeText = rawText.replace(/\n$/, '')
  const isBlock = !!match || rawText.includes('\n')

  const handleCopyCode = async (e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await navigator.clipboard.writeText(codeText)
      setCopied(true)
      message.success('代码已复制到剪贴板')
      setTimeout(() => setCopied(false), 2000)
    } catch {
      message.error('复制失败')
    }
  }

  if (!isBlock) {
    return (
      <code
        style={{
          background: 'var(--color-bg)',
          color: 'var(--color-primary)',
          padding: '2px 6px',
          borderRadius: 4,
          fontSize: '0.88em',
          fontFamily: 'Consolas, Monaco, "Courier New", monospace',
          border: '1px solid var(--color-border)',
        }}
        {...props}
      >
        {children}
      </code>
    )
  }

  return (
    <div
      style={{
        margin: '12px 0',
        borderRadius: 'var(--radius-md)',
        overflow: 'hidden',
        border: '1px solid var(--color-border)',
        background: 'var(--color-bg-white)',
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '4px 12px',
          background: 'var(--color-bg)',
          borderBottom: '1px solid var(--color-border)',
          fontSize: 12,
          color: 'var(--color-text-secondary)',
          fontFamily: 'Consolas, Monaco, monospace',
        }}
      >
        <span>{language || 'code'}</span>
        <Button
          type="text"
          size="small"
          icon={<CopyOutlined />}
          onClick={handleCopyCode}
          style={{ fontSize: 12, height: 24, padding: '0 6px' }}
        >
          {copied ? '已复制' : '复制'}
        </Button>
      </div>
      <pre
        style={{
          margin: 0,
          padding: '12px',
          overflowX: 'auto',
          fontSize: 13,
          lineHeight: 1.6,
          fontFamily: 'Consolas, Monaco, "Courier New", monospace',
          color: 'var(--color-text)',
        }}
      >
        <code>{children}</code>
      </pre>
    </div>
  )
}

const STRATEGY_LABELS: Record<string, string> = {
  structured_rag: '结构检索',
  agent: '多步查证',
}

/** 技术详情：正常态不占版面，展开才看策略、验证范围与定位精度。 */
const TechDetails: React.FC<{ finance: FinanceMessagePayload }> = ({ finance }) => {
  const verification = finance.verification
  const checked = verification?.checked_numbers || 0
  const issues = verification?.issues?.length || 0
  const counts: Record<string, number> = {}
  for (const citation of finance.citations) {
    const key = locatorPrecision(citation.evidence)
    counts[key] = (counts[key] || 0) + 1
  }
  const locators = [
    counts.cell ? `单元格 ${counts.cell}` : '',
    counts.rows ? `行组 ${counts.rows}` : '',
    counts.paragraph ? `段落 ${counts.paragraph}` : '',
    counts.document ? `整篇 ${counts.document}` : '',
  ]
    .filter(Boolean)
    .join(' · ')
  const strategy = STRATEGY_LABELS[finance.strategy] || finance.strategy
  return (
    <details className="tech-details">
      <summary>技术详情</summary>
      <div className="tech-details-body">
        <div>
          策略：{strategy}
          {finance.route_reason ? ` · ${finance.route_reason}` : ''}
        </div>
        <div>
          验证：
          {verification
            ? `已核验 ${checked} 项${issues ? `，待复核 ${issues} 项` : '，未发现不一致'}`
            : '未记录'}
        </div>
        <div>定位：{locators || '无引用'}</div>
        {finance.notes && finance.notes.length > 0 && (
          <div>备注：{finance.notes.join(' · ')}</div>
        )}
      </div>
    </details>
  )
}

const MessageItem: React.FC<MessageItemProps> = ({
  content,
  trace,
  finance,
  statusOverride,
  plain = false,
  onEvidenceClick,
  onCopy,
}) => (
  <div
    style={{
      background: plain ? 'transparent' : 'var(--color-bg-white)',
      border: plain ? 'none' : '1px solid var(--color-border)',
      borderRadius: plain ? 0 : 'var(--radius-lg)',
      padding: plain ? 0 : '12px 16px',
    }}
  >
    {finance && (
      <FinanceConclusion
        finance={finance}
        statusOverride={statusOverride}
        onEvidenceClick={(evidence, calc) => onEvidenceClick?.(evidence, calc)}
      />
    )}

    {/* 有结构化要点时，正文收进“完整回答”，避免同一段结论出现两遍。
        异常轮次默认展开：口径和限定条件正是复核时要看的。 */}
    {finance && finance.claims.length > 0 ? (
      <details
        open={statusOverride !== undefined && statusOverride !== 'answered'}
        style={{
          marginTop: 8,
          borderRadius: 'var(--radius-md)',
          border: '1px solid var(--color-border)',
          background: 'var(--color-bg)',
          padding: '6px 10px',
        }}
      >
        <summary
          style={{
            cursor: 'pointer',
            fontSize: 12,
            color: 'var(--color-text-secondary)',
            userSelect: 'none',
          }}
        >
          完整回答
        </summary>
        <div
          className="markdown-body"
          style={{ color: 'var(--color-text)', fontSize: 13, lineHeight: 1.7, marginTop: 8 }}
        >
          <ReactMarkdown components={{ code: CodeBlock }}>{content}</ReactMarkdown>
        </div>
      </details>
    ) : (
      <div className="markdown-body" style={{ color: 'var(--color-text)', fontSize: 14, lineHeight: 1.6 }}>
        <ReactMarkdown components={{ code: CodeBlock }}>{content}</ReactMarkdown>
      </div>
    )}

    <AgentTracePanel trace={trace} />

    {finance && <TechDetails finance={finance} />}

    <div
      style={{
        marginTop: 10,
        display: 'flex',
        justifyContent: 'flex-end',
        borderTop: '1px solid var(--color-border-light)',
        paddingTop: 8,
      }}
    >
      <Space size={2}>
        <Button
          type="text"
          size="small"
          icon={<CopyOutlined />}
          onClick={() => onCopy?.(content)}
          title="复制回答"
          style={{ color: 'var(--color-text-secondary)' }}
        />
      </Space>
    </div>
  </div>
)

export default MessageItem
