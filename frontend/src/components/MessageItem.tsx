import React from 'react'
import { Button, Space, message } from 'antd'
import { CopyOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { AgentTraceStep, CalcRecord, EvidenceRef } from '../api/client'
import type { FinanceMessagePayload } from '../hooks/useChat'
import type { TurnStatus } from '../utils/status'
import { locatorPrecision } from '../utils/citation'
import AgentTracePanel from './AgentTracePanel'
import FinanceConclusion from './FinanceConclusion'
import MarketBriefCard from './MarketBriefCard'

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
          fontSize: 13,
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
          style={{ fontSize: 13, height: 24, padding: '0 6px' }}
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
  market_brief: '公开信息综述',
  concept: '概念说明',
}

/**
 * 表格必须包一层横向滚动：财报定位表常是 4~5 列，右侧工作台只有几百像素，
 * 不包的话整块表格会把消息气泡撑破，长表 id 还会把列宽顶死。
 */
const markdownComponents = {
  code: CodeBlock,
  table: ({ children }: { children?: React.ReactNode }) => (
    <div style={{ overflowX: 'auto', margin: '8px 0' }}>
      <table>{children}</table>
    </div>
  ),
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
        <div>
          定位：
          {finance.strategy === 'concept'
            ? '概念问答不引用本地材料'
            : locators || '无引用'}
        </div>
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
}) => {
  const brief = finance?.market_brief
  // 概念题没有引用、没有要点，也不是查证结论；单独打标，避免和“已核验”混淆。
  const isConcept = finance?.strategy === 'concept'
  const conceptUsedWeb = isConcept && (finance?.notes || []).includes('concept:web_used')
  // 费率/规则题：答案本来就该来自公开资料，和「通用知识」不是一回事。
  const conceptIsFact = isConcept && (finance?.notes || []).includes('concept:web_fact')
  // 首段定论单独放大显示，正文里把它去掉，避免同一句话出现两遍。
  const answerLead = isConcept ? finance?.answer_lead || '' : ''
  const bodyContent = answerLead ? content.replace(answerLead, '').trimStart() : content
  const webSources = isConcept ? finance?.web_sources || [] : []
  return (
  <div
    style={{
      background: plain ? 'transparent' : 'var(--color-bg-white)',
      border: plain ? 'none' : '1px solid var(--color-border)',
      borderRadius: plain ? 0 : 'var(--radius-lg)',
      padding: plain ? 0 : '12px 16px',
    }}
  >
    {brief ? (
      <MarketBriefCard brief={brief} />
    ) : (
      <>
        {finance && !isConcept && (
          <FinanceConclusion
            finance={finance}
            statusOverride={statusOverride}
            onEvidenceClick={(evidence, calc) => onEvidenceClick?.(evidence, calc)}
          />
        )}

        {isConcept && (
          <div
            style={{
              display: 'inline-block',
              marginBottom: 8,
              padding: '2px 8px',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--color-bg)',
              border: '1px solid var(--color-border)',
              color: 'var(--color-text-secondary)',
              fontSize: 13,
              lineHeight: 1.6,
            }}
          >
            {conceptIsFact
              ? '公开信息 · 未核实'
              : `通用知识${conceptUsedWeb ? ' + 公开信息' : ''} · 未核实`}
          </div>
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
                fontSize: 13,
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
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={markdownComponents}
              >
                {content}
              </ReactMarkdown>
            </div>
          </details>
        ) : (
          <>
            {answerLead && (
              <div
                className="markdown-body"
                style={{
                  fontSize: 15,
                  lineHeight: 1.75,
                  color: 'var(--color-text)',
                  marginBottom: 8,
                }}
              >
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={markdownComponents}
                >
                  {answerLead}
                </ReactMarkdown>
              </div>
            )}
            <div className="markdown-body" style={{ color: 'var(--color-text)', fontSize: 14, lineHeight: 1.6 }}>
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={markdownComponents}
              >
                {bodyContent}
              </ReactMarkdown>
            </div>
          </>
        )}

        {webSources.length > 0 && (
          <details
            style={{
              marginTop: 10,
              paddingTop: 8,
              borderTop: '1px dashed var(--color-border)',
            }}
          >
            <summary
              style={{
                cursor: 'pointer',
                fontSize: 13,
                color: 'var(--color-text-secondary)',
                userSelect: 'none',
              }}
            >
              {conceptUsedWeb ? '引用来源' : '参考资料'} · {webSources.length} 篇
              {conceptUsedWeb ? '' : '（未逐条引用）'}
            </summary>
            <ol style={{ margin: '6px 0 0', paddingLeft: 20, fontSize: 13, lineHeight: 1.8 }}>
              {webSources.map((source) => (
                <li key={source.url || source.title}>
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noreferrer"
                    style={{ color: 'var(--color-primary)' }}
                  >
                    {source.title || source.label}
                  </a>
                  <span style={{ color: 'var(--color-text-secondary)' }}>
                    {' · '}
                    {source.label}
                    {source.published_at ? ` ${source.published_at}` : ''}
                  </span>
                </li>
              ))}
            </ol>
          </details>
        )}
      </>
    )}

    <AgentTracePanel trace={trace} />

    {finance && !brief && <TechDetails finance={finance} />}

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
}

export default MessageItem
