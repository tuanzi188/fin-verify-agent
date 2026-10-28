import React, { useState, useMemo } from 'react'
import { Button, Typography, Space, Tag, message, Popover } from 'antd'
import {
  CopyOutlined,
  CheckOutlined,
  PaperClipOutlined,
  BulbOutlined,
  DownOutlined,
  UpOutlined,
  FileTextOutlined,
  RightOutlined,
} from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import { DisplayCitation, AgentStatus, AgentTraceStep } from '../api/client'
import AgentTracePanel from './AgentTracePanel'

const { Text } = Typography

interface MessageItemProps {
  role: 'user' | 'assistant'
  content: string
  citations?: DisplayCitation[]
  confidence?: number
  model?: string
  mode?: string
  reasoning_content?: string
  isFallback?: boolean
  status?: AgentStatus
  trace?: AgentTraceStep[]
  onCitationClick?: (citation: DisplayCitation) => void
  onCopy?: (text: string) => void
}

const STATUS_META: Record<AgentStatus, { label: string; color: string }> = {
  answered: { label: '已回答', color: 'green' },
  abstained: { label: '资料不足', color: 'orange' },
  clarified: { label: '需要澄清', color: 'blue' },
  error: { label: '执行失败', color: 'red' },
}

const getConfidenceColor = (score: number): string => {
  if (score >= 0.7) return 'green'
  if (score >= 0.4) return 'orange'
  return 'red'
}

/** 智能格式化行内引用标记，转换为 Markdown 链接以便组件捕获 */
function formatCitationLinks(text: string): string {
  if (!text) return ''

  // 1. 匹配 [引用: 1] 或 [引用: 1, 2, 3] / [引用: 1、2]
  let formatted = text.replace(/\[引用:\s*([\d\s,，、]+)\]/g, (_, nums) => {
    const indices = nums.split(/[,，、\s]+/).filter(Boolean)
    return indices.map((n: string) => `[^cite-${n}](#cite-${n})`).join(' ')
  })

  // 2. 匹配 [^1] 或 [^2] 类似角标
  formatted = formatted.replace(/\[\^(\d+)\](?!\()/g, (_, num) => {
    return `[^cite-${num}](#cite-${num})`
  })

  // 3. Agent 直接输出 [1] / [2]：转成同样的胶囊链接。
  //    排除 Markdown 链接写法 [1](url)，否则会把链接文字吞掉。
  formatted = formatted.replace(/\[(\d{1,2})\](?!\()/g, (_, num) => {
    return `[^cite-${num}](#cite-${num})`
  })

  return formatted
}

/** 创新交互：行内引用胶囊与悬浮即时验真卡片 (In-text Citation Pill & Hover Preview) */
const CitationPill: React.FC<{
  index: number
  citation?: DisplayCitation
  onClick?: () => void
}> = ({ index, citation, onClick }) => {
  if (!citation) {
    return (
      <span
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '0 5px',
          height: 18,
          fontSize: 11,
          borderRadius: 9,
          background: 'var(--color-primary-light)',
          color: 'var(--color-primary)',
          fontWeight: 600,
          margin: '0 2px',
          verticalAlign: 'text-top',
          cursor: 'default',
        }}
      >
        [{index}]
      </span>
    )
  }

  const hasScore = typeof citation.score === 'number'
  const scorePercent = hasScore
    ? Math.min(100, Math.round((citation.score as number) * 100))
    : null

  const popoverContent = (
    <div style={{ maxWidth: 300, padding: '4px 2px' }}>
      {/* 头部：文档名与匹配率 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8, gap: 8 }}>
        <Space size={6} style={{ minWidth: 0, flex: 1 }}>
          <FileTextOutlined style={{ color: 'var(--color-primary)', flexShrink: 0 }} />
          <Text strong ellipsis style={{ fontSize: 13, maxWidth: 180 }}>
            {citation.document_title}
          </Text>
        </Space>
        {scorePercent !== null && (
          <Tag
            color={scorePercent >= 70 ? 'green' : 'orange'}
            style={{ margin: 0, fontSize: 11, borderRadius: 4, flexShrink: 0 }}
          >
            匹配 {scorePercent}%
          </Tag>
        )}
      </div>

      {/* 原段切片高光卡片 */}
      <div
        style={{
          background: 'var(--color-bg)',
          borderRadius: 6,
          padding: '8px 10px',
          fontSize: 12,
          lineHeight: 1.6,
          color: 'var(--color-text)',
          maxHeight: 130,
          overflowY: 'auto',
          border: '1px solid var(--color-border)',
          marginBottom: 10,
        }}
      >
        <span style={{ color: 'var(--color-text-secondary)', display: 'block', fontSize: 11, marginBottom: 2 }}>
          知识库原文切片:
        </span>
        {citation.content.length > 140 ? citation.content.slice(0, 140) + '...' : citation.content}
      </div>

      {/* 底部定位与跳转 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Space size={6} style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }}>
          {citation.page !== undefined && citation.page !== null && <span>P.{citation.page}</span>}
          {citation.paragraph !== undefined && citation.paragraph !== null && <span>§{citation.paragraph}</span>}
        </Space>
        <Button
          type="link"
          size="small"
          onClick={onClick}
          style={{ padding: 0, fontSize: 12, height: 'auto', display: 'flex', alignItems: 'center', gap: 2 }}
        >
          <span>查看完整上下文</span>
          <RightOutlined style={{ fontSize: 10 }} />
        </Button>
      </div>
    </div>
  )

  return (
    <Popover
      content={popoverContent}
      title={null}
      trigger="hover"
      placement="top"
      mouseEnterDelay={0.12}
    >
      <span
        onClick={(e) => {
          e.preventDefault()
          e.stopPropagation()
          onClick?.()
        }}
        className="btn-hover"
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
          border: '1px solid rgba(22, 119, 255, 0.25)',
          userSelect: 'none',
          boxShadow: 'var(--shadow-sm)',
        }}
      >
        <FileTextOutlined style={{ fontSize: 10 }} />
        <span>{index}</span>
      </span>
    </Popover>
  )
}

/** 自定义代码块组件：带语言标签与一键复制代码 */
const CodeBlock: React.FC<{
  className?: string
  children?: React.ReactNode
}> = ({ className, children, ...props }) => {
  const [copied, setCopied] = useState(false)
  const match = /language-(\w+)/.exec(className || '')
  const language = match ? match[1] : ''
  const rawText = String(children)
  const codeText = rawText.replace(/\n$/, '')
  // react-markdown v9 起不再向 code 组件传 inline，改为「有 language- 类名」或「内容含换行」判块级，
  // 否则行内代码（如 `500 元`）会被误渲染成带标题栏和复制按钮的整块代码框。
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
        boxShadow: 'var(--shadow-sm)',
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
          icon={copied ? <CheckOutlined style={{ color: 'var(--color-success)' }} /> : <CopyOutlined />}
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
          background: 'transparent',
        }}
      >
        <code>{children}</code>
      </pre>
    </div>
  )
}

/** 深度思考 (DeepSeek Reasoning) 展开/收起组件 */
const ReasoningSection: React.FC<{ reasoning: string }> = ({ reasoning }) => {
  const [expanded, setExpanded] = useState(false)

  if (!reasoning || !reasoning.trim()) return null

  return (
    <div
      style={{
        marginBottom: 12,
        borderRadius: 'var(--radius-md)',
        background: 'var(--color-bg)',
        border: '1px solid var(--color-border)',
        overflow: 'hidden',
      }}
    >
      <div
        onClick={() => setExpanded(!expanded)}
        style={{
          padding: '8px 12px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          cursor: 'pointer',
          userSelect: 'none',
          fontSize: 12,
          color: 'var(--color-text-secondary)',
          background: 'rgba(0, 0, 0, 0.02)',
        }}
      >
        <Space size={6}>
          <BulbOutlined style={{ color: 'var(--color-primary)' }} />
          <span style={{ fontWeight: 500, color: 'var(--color-text)' }}>深度思考过程 (Reasoning)</span>
        </Space>
        <Space size={4}>
          <span style={{ fontSize: 11 }}>{expanded ? '收起思考' : '展开思考'}</span>
          {expanded ? <UpOutlined style={{ fontSize: 10 }} /> : <DownOutlined style={{ fontSize: 10 }} />}
        </Space>
      </div>
      {expanded && (
        <div
          style={{
            padding: '10px 12px',
            borderTop: '1px solid var(--color-border)',
            borderLeft: '3px solid var(--color-primary)',
            fontSize: 13,
            lineHeight: 1.6,
            color: 'var(--color-text-secondary)',
            maxHeight: 360,
            overflowY: 'auto',
            background: 'var(--color-bg-white)',
          }}
        >
          <div className="markdown-body">
            <ReactMarkdown
              components={{
                code: CodeBlock,
              }}
            >
              {reasoning}
            </ReactMarkdown>
          </div>
        </div>
      )}
    </div>
  )
}

const MessageItem: React.FC<MessageItemProps> = ({
  role,
  content,
  citations,
  confidence,
  model,
  mode,
  reasoning_content,
  isFallback,
  status,
  trace,
  onCitationClick,
  onCopy,
}) => {
  const isUser = role === 'user'

  // 格式化文本中的行内引用标记
  const formattedContent = useMemo(() => {
    return isUser ? content : formatCitationLinks(content)
  }, [content, isUser])

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: isUser ? 'row-reverse' : 'row',
        marginBottom: 16,
        gap: 10,
        alignItems: 'flex-start',
      }}
    >
      {/* 头像 */}
      <div
        style={{
          width: 34,
          height: 34,
          borderRadius: isUser ? '50%' : 'var(--radius-md)',
          background: isUser
            ? 'linear-gradient(135deg, #1677ff 0%, #0958d9 100%)'
            : 'linear-gradient(135deg, #52c41a 0%, #389e0d 100%)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#fff',
          fontSize: 14,
          fontWeight: 600,
          flexShrink: 0,
          boxShadow: 'var(--shadow-sm)',
        }}
      >
        {isUser ? 'U' : 'K'}
      </div>

      {/* 气泡正文 */}
      <div
        style={{
          maxWidth: '85%',
          background: isUser ? 'var(--color-primary)' : 'var(--color-bg-white)',
          color: isUser ? '#ffffff' : 'var(--color-text)',
          borderRadius: 'var(--radius-lg)',
          padding: '12px 16px',
          boxShadow: 'var(--shadow-sm)',
          border: isUser ? 'none' : '1px solid var(--color-border)',
          transition: 'background var(--transition-normal), border var(--transition-normal)',
        }}
      >
        {isUser ? (
          <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 14, lineHeight: 1.6 }}>
            {content}
          </div>
        ) : (
          <>
            {/* 思考过程 */}
            {reasoning_content && <ReasoningSection reasoning={reasoning_content} />}

            {/* 回答正文 Markdown */}
            <div className="markdown-body" style={{ color: 'var(--color-text)', fontSize: 14, lineHeight: 1.6 }}>
              <ReactMarkdown
                components={{
                  code: CodeBlock,
                  a: ({ href, children }) => {
                    if (href?.startsWith('#cite-')) {
                      const idxStr = href.replace('#cite-', '')
                      const idx = parseInt(idxStr, 10)
                      const targetCitation = citations && !isNaN(idx) ? citations[idx - 1] : undefined
                      return (
                        <CitationPill
                          index={idx || 1}
                          citation={targetCitation}
                          onClick={() => targetCitation && onCitationClick?.(targetCitation)}
                        />
                      )
                    }
                    return (
                      <a href={href} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--color-primary)' }}>
                        {children}
                      </a>
                    )
                  },
                }}
              >
                {formattedContent}
              </ReactMarkdown>
            </div>

            {/* 引用来源 Tags */}
            {citations && citations.length > 0 && (
              <div
                style={{
                  marginTop: 12,
                  borderTop: '1px solid var(--color-border-light)',
                  paddingTop: 8,
                }}
              >
                <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginBottom: 6 }}>
                  引用知识来源 ({citations.length}):
                </div>
                <Space wrap size={[6, 6]}>
                  {citations.slice(0, 4).map((cit, idx) => (
                    <Tag
                      key={idx}
                      color="blue"
                      style={{
                        cursor: 'pointer',
                        margin: 0,
                        borderRadius: 4,
                        padding: '2px 8px',
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 4,
                      }}
                      onClick={() => onCitationClick?.(cit)}
                    >
                      <PaperClipOutlined />
                      <span>
                        {cit.document_title.length > 24
                          ? cit.document_title.slice(0, 24) + '...'
                          : cit.document_title}
                      </span>
                    </Tag>
                  ))}
                  {citations.length > 4 && (
                    <Tag color="default" style={{ margin: 0, borderRadius: 4 }}>
                      +{citations.length - 4} 更多
                    </Tag>
                  )}
                </Space>
              </div>
            )}

            {!isUser && <AgentTracePanel trace={trace} />}

            {/* 底部信息条：模型、置信度、操作按钮 */}
            <div
              style={{
                marginTop: 10,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                flexWrap: 'wrap',
                gap: 8,
                borderTop: '1px solid var(--color-border-light)',
                paddingTop: 8,
              }}
            >
              <Space size={6} wrap style={{ fontSize: 11 }}>
                {status && (
                  <Tag color={STATUS_META[status].color} style={{ fontSize: 11, margin: 0 }}>
                    {STATUS_META[status].label}
                  </Tag>
                )}
                {isFallback && <Tag color="orange" style={{ fontSize: 11, margin: 0 }}>兜底回答</Tag>}
                {confidence !== undefined && (
                  <Tag color={getConfidenceColor(confidence)} style={{ fontSize: 11, margin: 0 }}>
                    置信度: {(confidence * 100).toFixed(0)}%
                  </Tag>
                )}
                {model && <Text style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }}>{model}</Text>}
                {mode && <Text style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }}>{mode}</Text>}
              </Space>

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
          </>
        )}
      </div>
    </div>
  )
}

export default MessageItem
