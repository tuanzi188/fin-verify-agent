import React from 'react'
import { Button, Space, message } from 'antd'
import { CopyOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import type { AgentTraceStep, CalcRecord, EvidenceRef } from '../api/client'
import type { FinanceMessagePayload } from '../hooks/useChat'
import AgentTracePanel from './AgentTracePanel'
import FinanceConclusion from './FinanceConclusion'

interface MessageItemProps {
  content: string
  trace?: AgentTraceStep[]
  finance?: FinanceMessagePayload
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

const MessageItem: React.FC<MessageItemProps> = ({
  content,
  trace,
  finance,
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
      <FinanceConclusion finance={finance} onEvidenceClick={(evidence, calc) => onEvidenceClick?.(evidence, calc)} />
    )}

    {/* 有结构化要点时，正文收进“完整回答”，避免同一段结论出现两遍。 */}
    {finance && finance.claims.length > 0 ? (
      <details
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
          完整回答（含口径与限定条件）
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
