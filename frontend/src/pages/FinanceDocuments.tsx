import React, { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Input, Space, Spin, Table, Typography, Upload, message } from 'antd'
import { FolderOpenOutlined, UploadOutlined } from '@ant-design/icons'
import {
  FinanceDocumentInfo,
  getFinanceDocuments,
  uploadFinanceDocument,
} from '../api/client'

const { Title, Paragraph } = Typography

const docTypeLabel = (value: string): string => {
  if (value === 'annual_report') return '年报'
  if (value === 'prospectus') return '募集说明书'
  return value || '—'
}

/** 解析状态：给查证用户看的是"能不能用"，不是解析器的内部枚举。 */
const statusLabel = (value: string): string => {
  if (value === 'parsed') return '已解析'
  if (value === 'failed') return '解析失败'
  if (value === 'pending') return '待解析'
  return value || '—'
}

const PlainTag: React.FC<{ children: React.ReactNode; tone?: 'muted' | 'ok' | 'warn' }> = ({
  children,
  tone = 'muted',
}) => {
  const color =
    tone === 'ok'
      ? 'var(--color-success)'
      : tone === 'warn'
        ? 'var(--color-review)'
        : 'var(--color-text-secondary)'
  return (
    <span
      style={{
        display: 'inline-block',
        padding: '1px 8px',
        borderRadius: 'var(--radius-sm)',
        border: '1px solid var(--color-border)',
        color,
        fontSize: 12,
        lineHeight: 1.7,
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  )
}

const FinanceDocuments: React.FC = () => {
  const [documents, setDocuments] = useState<FinanceDocumentInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [company, setCompany] = useState('')
  const [period, setPeriod] = useState('')
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)

  const loadDocuments = useCallback(() => {
    setLoading(true)
    getFinanceDocuments()
      .then((data) => {
        setDocuments(data.documents || [])
        setError(null)
      })
      .catch((err) => setError(err?.response?.data?.detail || err?.message || '加载失败'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    loadDocuments()
  }, [loadDocuments])

  const handleUpload = async (file: File) => {
    setUploading(true)
    setUploadError(null)
    try {
      const result = await uploadFinanceDocument(file, { company, period })
      if (result.warnings?.length) {
        message.warning(`${result.status}：${result.warnings.join('；')}`)
      } else {
        message.success(`${result.status}：${result.tables} 张表 / ${result.sections} 个章节`)
      }
      setCompany('')
      setPeriod('')
      loadDocuments()
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        (err as Error)?.message ||
        '上传失败'
      setUploadError(detail)
      message.error(detail)
    } finally {
      setUploading(false)
    }
  }

  const sections = documents.reduce((sum, doc) => sum + (doc.sections || 0), 0)
  const tables = documents.reduce((sum, doc) => sum + (doc.tables || 0), 0)

  const columns = [
    {
      title: '公司',
      dataIndex: 'company',
      key: 'company',
      width: 140,
      render: (value: string, row: FinanceDocumentInfo) => (
        <div>
          <div style={{ fontWeight: 600, fontSize: 13 }}>{value || '—'}</div>
          <div style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>{row.code || ''}</div>
        </div>
      ),
    },
    {
      title: '类型',
      dataIndex: 'doc_type',
      key: 'doc_type',
      width: 110,
      render: (value: string) => <PlainTag>{docTypeLabel(value)}</PlainTag>,
    },
    { title: '报告期', dataIndex: 'period', key: 'period', width: 80 },
    {
      title: '披露日期',
      key: 'published_at',
      width: 110,
      render: (_: unknown, row: FinanceDocumentInfo) =>
        row.published_at || row.announcement_date || '—',
    },
    {
      title: '页码',
      key: 'pages',
      width: 100,
      render: (_: unknown, row: FinanceDocumentInfo) =>
        row.page_start && row.page_end ? `P.${row.page_start}–${row.page_end}` : '—',
    },
    {
      title: '表格',
      dataIndex: 'tables',
      key: 'tables',
      width: 80,
      align: 'right' as const,
      render: (value: number) => <span className="num" style={{ display: 'block' }}>{value ?? '—'}</span>,
    },
    {
      title: '默认单位',
      dataIndex: 'unit',
      key: 'unit',
      width: 100,
      render: (value: string) => (
        <span title="文档级默认值；同一份报告的不同表格可能各自标注单位">
          {value || '—'}
        </span>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (value: string) => (
        <PlainTag tone={value === 'parsed' ? 'ok' : 'warn'}>{statusLabel(value)}</PlainTag>
      ),
    },
  ]

  return (
    <div style={{ padding: '20px 24px', maxWidth: 1180, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <FolderOpenOutlined style={{ color: 'var(--color-primary)', fontSize: 20 }} />
        <Title level={3} style={{ margin: 0 }}>语料覆盖</Title>
      </div>
      <Paragraph type="secondary" style={{ marginTop: 6, fontSize: 13 }}>
        官方语料是固定的本地文档库；你也可以上传 PDF 临时扩大可查范围。展开任意一行可以看到文档
        ID、披露来源和解析器版本。
      </Paragraph>

      <Card size="small" style={{ marginBottom: 12 }}>
        <Space wrap>
          <Input
            placeholder="公司（可选）"
            value={company}
            onChange={(event) => setCompany(event.target.value)}
            style={{ width: 160 }}
            disabled={uploading}
          />
          <Input
            placeholder="报告期，如 2025（可选）"
            value={period}
            onChange={(event) => setPeriod(event.target.value)}
            style={{ width: 180 }}
            disabled={uploading}
          />
          <Upload
            accept=".pdf"
            showUploadList={false}
            disabled={uploading}
            beforeUpload={(file) => {
              void handleUpload(file as File)
              return false
            }}
          >
            <Button icon={<UploadOutlined />} loading={uploading}>
              上传 PDF
            </Button>
          </Upload>
          <span style={{ fontSize: 13, color: 'var(--color-text-tertiary)' }}>
            仅支持文字版 PDF；扫描件暂不支持
          </span>
        </Space>
        {uploadError ? (
          <Alert type="error" showIcon message={uploadError} style={{ marginTop: 8 }} />
        ) : null}
      </Card>

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
            <PlainTag tone="ok">{documents.length} 份文档</PlainTag>
            <PlainTag>{sections} 个章节</PlainTag>
            <PlainTag>{tables} 张表</PlainTag>
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
              expandable={{
                expandedRowRender: (row) => (
                  <div style={{ fontSize: 13, lineHeight: 1.9, color: 'var(--color-text-secondary)' }}>
                    <div>
                      文档 ID：<span style={{ fontFamily: 'var(--font-numeric)' }}>{row.doc_id}</span>
                    </div>
                    <div>章节数：{row.sections ?? '—'}</div>
                    <div>来源：{row.origin === 'upload' ? '用户上传' : '官方语料'}</div>
                    <div>解析版本：{row.pipeline_version || '—'}</div>
                    {row.source_url ? (
                      <div>
                        原始披露：
                        <a href={row.source_url} target="_blank" rel="noreferrer">
                          {row.source_url}
                        </a>
                      </div>
                    ) : (
                      <div>原始披露：未记录</div>
                    )}
                  </div>
                ),
              }}
            />
          </Card>
        </>
      )}
    </div>
  )
}

export default FinanceDocuments
