import React, { useEffect, useState } from 'react'
import { Alert, Card, Space, Spin, Table, Tag, Typography } from 'antd'
import { FileTextOutlined, FolderOpenOutlined } from '@ant-design/icons'
import { FinanceDocumentInfo, getFinanceDocuments } from '../api/client'

const { Title, Text, Paragraph } = Typography

const docTypeLabel = (value: string): string => {
  if (value === 'annual_report') return '年报'
  if (value === 'prospectus') return '募集说明书'
  return value || '—'
}

const FinanceDocuments: React.FC = () => {
  const [documents, setDocuments] = useState<FinanceDocumentInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getFinanceDocuments()
      .then((data) => setDocuments(data.documents || []))
      .catch((err) => setError(err?.response?.data?.detail || err?.message || '加载失败'))
      .finally(() => setLoading(false))
  }, [])

  const sections = documents.reduce((sum, doc) => sum + (doc.sections || 0), 0)
  const tables = documents.reduce((sum, doc) => sum + (doc.tables || 0), 0)

  const columns = [
    {
      title: '公司',
      dataIndex: 'company',
      key: 'company',
      width: 120,
      render: (value: string, row: FinanceDocumentInfo) => (
        <div>
          <div style={{ fontWeight: 600, fontSize: 13 }}>{value || '—'}</div>
          <Text type="secondary" style={{ fontSize: 10 }}>{row.code || row.doc_id}</Text>
        </div>
      ),
    },
    { title: '报告期', dataIndex: 'period', key: 'period', width: 80 },
    {
      title: '类型',
      dataIndex: 'doc_type',
      key: 'doc_type',
      width: 110,
      render: (value: string) => <Tag color={value === 'annual_report' ? 'blue' : 'purple'}>{docTypeLabel(value)}</Tag>,
    },
    {
      title: '页码',
      key: 'pages',
      width: 100,
      render: (_: unknown, row: FinanceDocumentInfo) =>
        row.page_start && row.page_end ? `P.${row.page_start}–${row.page_end}` : '—',
    },
    { title: '章节', dataIndex: 'sections', key: 'sections', width: 80 },
    { title: '表格', dataIndex: 'tables', key: 'tables', width: 80 },
    { title: '单位', dataIndex: 'unit', key: 'unit', width: 80 },
    {
      title: '文档',
      dataIndex: 'doc_id',
      key: 'doc_id',
      ellipsis: true,
      render: (value: string) => <Text code style={{ fontSize: 11 }}>{value}</Text>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (value: string) => <Tag color={value === 'parsed' ? 'green' : 'orange'}>{value}</Tag>,
    },
  ]

  return (
    <div style={{ padding: '20px 24px', maxWidth: 1180, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <FolderOpenOutlined style={{ color: 'var(--color-primary)', fontSize: 20 }} />
        <Title level={3} style={{ margin: 0 }}>语料覆盖</Title>
      </div>
      <Paragraph type="secondary" style={{ marginTop: 6, fontSize: 13 }}>
        金融语料是固定的本地文档库；这里的章节、表格、页码都来自解析产物，不把片段数当文档数。
      </Paragraph>

      {loading ? (
        <div style={{ padding: 48, textAlign: 'center' }}><Spin /></div>
      ) : error ? (
        <Alert
          type="warning"
          showIcon
          message="金融语料未就绪"
          description={error}
        />
      ) : (
        <>
          <Space wrap size={[8, 8]} style={{ marginBottom: 12 }}>
            <Tag icon={<FileTextOutlined />} color="blue">{documents.length} 份文档</Tag>
            <Tag>{sections} 个章节</Tag>
            <Tag>{tables} 张表</Tag>
          </Space>
          <Card size="small" styles={{ body: { padding: 0 } }}>
            <Table
              rowKey="doc_id"
              size="small"
              loading={loading}
              dataSource={documents}
              columns={columns}
              pagination={false}
              scroll={{ x: 900 }}
            />
          </Card>
        </>
      )}
    </div>
  )
}

export default FinanceDocuments
