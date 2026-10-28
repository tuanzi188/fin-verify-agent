import React, { useState } from 'react'
import { Drawer, Typography, Tag, Space, Button, message } from 'antd'
import {
  CloseOutlined,
  FileTextOutlined,
  CopyOutlined,
  CheckOutlined,
} from '@ant-design/icons'
import { DisplayCitation } from '../api/client'

const { Text, Title, Paragraph } = Typography

interface CitationPanelProps {
  citation: DisplayCitation | null
  onClose: () => void
}

const CitationPanel: React.FC<CitationPanelProps> = ({ citation, onClose }) => {
  const [copied, setCopied] = useState(false)

  const handleCopyContent = async () => {
    if (!citation?.content) return
    try {
      await navigator.clipboard.writeText(citation.content)
      setCopied(true)
      message.success('引用段落已复制')
      setTimeout(() => setCopied(false), 2000)
    } catch {
      message.error('复制失败')
    }
  }

  return (
    <Drawer
      title={
        <Space>
          <FileTextOutlined style={{ color: 'var(--color-primary)' }} />
          <span style={{ fontWeight: 600 }}>知识溯源与引用详情</span>
        </Space>
      }
      placement="right"
      width={420}
      onClose={onClose}
      open={!!citation}
      extra={<Button type="text" icon={<CloseOutlined />} onClick={onClose} />}
    >
      {citation && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {/* 文档名称 */}
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>来源文档</Text>
            <Title level={5} style={{ margin: '4px 0 0', wordBreak: 'break-all' }}>
              {citation.document_title}
            </Title>
          </div>

          {/* 属性标签 */}
          <div
            style={{
              padding: '12px',
              borderRadius: 'var(--radius-md)',
              background: 'var(--color-bg)',
              border: '1px solid var(--color-border)',
            }}
          >
            <Space wrap size={[6, 6]}>
              {citation.page !== undefined && citation.page !== null && (
                <Tag color="blue" style={{ margin: 0, borderRadius: 4 }}>
                  页码: {citation.page}
                </Tag>
              )}
              {citation.paragraph !== undefined && citation.paragraph !== null && (
                <Tag color="green" style={{ margin: 0, borderRadius: 4 }}>
                  段落: {citation.paragraph}
                </Tag>
              )}
              {citation.document_id && (
                <Tag style={{ margin: 0, borderRadius: 4 }}>
                  Doc: {citation.document_id.slice(0, 8)}...
                </Tag>
              )}
            </Space>
          </div>

          {/* 引用段落正文 */}
          <div
            style={{
              background: 'var(--color-bg)',
              borderRadius: 'var(--radius-md)',
              border: '1px solid var(--color-border)',
              padding: '12px 14px',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
              <Text type="secondary" style={{ fontSize: 12, fontWeight: 500 }}>
                召回知识切片 (Chunk Content)
              </Text>
              <Button
                type="text"
                size="small"
                icon={copied ? <CheckOutlined style={{ color: 'var(--color-success)' }} /> : <CopyOutlined />}
                onClick={handleCopyContent}
                style={{ fontSize: 12, height: 24, padding: '0 6px' }}
              >
                {copied ? '已复制' : '复制文本'}
              </Button>
            </div>
            <Paragraph
              style={{
                marginBottom: 0,
                fontSize: 13,
                lineHeight: 1.7,
                whiteSpace: 'pre-wrap',
                color: 'var(--color-text)',
                maxHeight: 280,
                overflowY: 'auto',
              }}
            >
              {citation.content}
            </Paragraph>
          </div>
        </div>
      )}
    </Drawer>
  )
}

export default CitationPanel
