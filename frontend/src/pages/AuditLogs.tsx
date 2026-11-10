import React, { useEffect, useState } from 'react'
import {
  Card,
  Form,
  Input,
  Select,
  DatePicker,
  Button,
  Table,
  Tag,
  Space,
  Typography,
  Spin,
  Alert,
  Pagination,
  Descriptions,
} from 'antd'
import { SearchOutlined, ReloadOutlined, SafetyOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { queryAuditLogs, AuditLogQueryResponse } from '../api/client'
import { useUser } from '../contexts/UserContext'

const { Title, Text } = Typography
const { RangePicker } = DatePicker
const { Option } = Select

const PAGE_SIZE = 20

const STATUS_OPTIONS = [
  { value: 'success', label: '成功', color: 'success' },
  { value: 'failure', label: '失败', color: 'error' },
  { value: 'denied', label: '拒绝', color: 'warning' },
]

const ACTION_OPTIONS = [
  'DOCUMENT_UPLOAD',
  'DOCUMENT_LIST',
  'DOCUMENT_DELETE',
  'CHAT',
  'CHAT_STREAM',
  'SEARCH',
  'CONVERSATION_VIEW',
  'CONVERSATION_DELETE',
  'ANALYTICS_VIEW',
]

const RESOURCE_OPTIONS = ['document', 'conversation', 'chat', 'search', 'analytics', 'system']

const AuditLogs: React.FC = () => {
  const { isAdmin } = useUser()
  const [form] = Form.useForm()
  const [data, setData] = useState<AuditLogQueryResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string>()
  const [offset, setOffset] = useState(0)

  const fetchLogs = async (nextOffset: number = 0) => {
    setLoading(true)
    setError(undefined)
    try {
      const values = form.getFieldsValue()
      const timeRange = values.timeRange
      const result = await queryAuditLogs({
        user_id: values.user_id?.trim() || undefined,
        action: values.action || undefined,
        resource_type: values.resource_type || undefined,
        resource_id: values.resource_id?.trim() || undefined,
        status: values.status || undefined,
        start_time: timeRange?.[0]?.toISOString(),
        end_time: timeRange?.[1]?.toISOString(),
        limit: PAGE_SIZE,
        offset: nextOffset,
      })
      setData(result)
      setOffset(nextOffset)
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || '查询失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (isAdmin) {
      fetchLogs(0)
    }
  }, [isAdmin])

  if (!isAdmin) {
    return (
      <div style={{ padding: 48, textAlign: 'center', maxWidth: 600, margin: '0 auto' }}>
        <Alert
          message="权限不足"
          description="审计日志仅管理员可查看。请在用户设置中将角色设为 admin 后刷新页面。"
          type="warning"
          showIcon
        />
      </div>
    )
  }

  const columns = [
    {
      title: '时间',
      dataIndex: 'timestamp',
      key: 'timestamp',
      width: 180,
      render: (value: number | string) => {
        try {
          return dayjs(value).format('YYYY-MM-DD HH:mm:ss')
        } catch {
          return String(value)
        }
      },
    },
    {
      title: '用户',
      dataIndex: 'user_id',
      key: 'user_id',
      width: 140,
      render: (value: unknown, record: Record<string, unknown>) => (
        <div>
          <Text strong>{String(value || '-')}</Text>
          {Boolean(record.department) && (
            <div style={{ fontSize: 13, color: 'var(--color-text-secondary)' }}>
              {String(record.department)}
            </div>
          )}
        </div>
      ),
    },
    {
      title: '操作',
      dataIndex: 'action',
      key: 'action',
      width: 160,
      render: (value: string) => <Tag>{value}</Tag>,
    },
    {
      title: '资源类型',
      dataIndex: 'resource_type',
      key: 'resource_type',
      width: 120,
    },
    {
      title: '资源 ID',
      dataIndex: 'resource_id',
      key: 'resource_id',
      width: 180,
      ellipsis: true,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (value: string) => {
        const option = STATUS_OPTIONS.find((o) => o.value === value)
        return <Tag color={option?.color}>{option?.label || value}</Tag>
      },
    },
    {
      title: '耗时 (ms)',
      dataIndex: 'duration_ms',
      key: 'duration_ms',
      width: 100,
      render: (value: number) => (typeof value === 'number' ? `${value.toFixed(1)}` : '-'),
    },
  ]

  const expandedRowRender = (record: Record<string, unknown>) => {
    const roles = record.roles
    return (
      <Descriptions bordered size="small" column={2}>
        <Descriptions.Item label="IP 地址">{String(record.ip_address || '-')}</Descriptions.Item>
        <Descriptions.Item label="User Agent">{String(record.user_agent || '-')}</Descriptions.Item>
        <Descriptions.Item label="角色">{Array.isArray(roles) ? roles.join(', ') : '-'}</Descriptions.Item>
        <Descriptions.Item label="部门">{String(record.department || '-')}</Descriptions.Item>
        {Boolean(record.error_message) && (
          <Descriptions.Item label="错误信息" span={2}>
            <Text type="danger">{String(record.error_message)}</Text>
          </Descriptions.Item>
        )}
        {Boolean(record.details) && (
          <Descriptions.Item label="详情" span={2}>
            <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
              {JSON.stringify(record.details, null, 2)}
            </pre>
          </Descriptions.Item>
        )}
      </Descriptions>
    )
  }

  return (
    <div style={{ padding: 24, maxWidth: 1440, margin: '0 auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>
          <SafetyOutlined style={{ marginRight: 12 }} />
          审计日志
        </Title>
        <Button icon={<ReloadOutlined />} onClick={() => fetchLogs(0)} loading={loading}>
          刷新
        </Button>
      </div>

      <Card style={{ marginBottom: 24, background: 'var(--color-bg-white)' }}>
        <Form form={form} layout="inline" onFinish={() => fetchLogs(0)}>
          <Form.Item name="user_id" label="用户 ID">
            <Input placeholder="用户 ID" allowClear />
          </Form.Item>

          <Form.Item name="action" label="操作">
            <Select placeholder="选择操作" allowClear style={{ width: 160 }}>
              {ACTION_OPTIONS.map((action) => (
                <Option key={action} value={action}>
                  {action}
                </Option>
              ))}
            </Select>
          </Form.Item>

          <Form.Item name="resource_type" label="资源类型">
            <Select placeholder="选择资源类型" allowClear style={{ width: 140 }}>
              {RESOURCE_OPTIONS.map((type) => (
                <Option key={type} value={type}>
                  {type}
                </Option>
              ))}
            </Select>
          </Form.Item>

          <Form.Item name="resource_id" label="资源 ID">
            <Input placeholder="资源 ID" allowClear />
          </Form.Item>

          <Form.Item name="status" label="状态">
            <Select placeholder="选择状态" allowClear style={{ width: 120 }}>
              {STATUS_OPTIONS.map((option) => (
                <Option key={option.value} value={option.value}>
                  {option.label}
                </Option>
              ))}
            </Select>
          </Form.Item>

          <Form.Item name="timeRange" label="时间范围">
            <RangePicker showTime format="YYYY-MM-DD HH:mm" />
          </Form.Item>

          <Form.Item>
            <Space>
              <Button type="primary" htmlType="submit" icon={<SearchOutlined />} loading={loading}>
                查询
              </Button>
              <Button onClick={() => { form.resetFields(); fetchLogs(0) }}>
                重置
              </Button>
            </Space>
          </Form.Item>
        </Form>
      </Card>

      {error && (
        <Alert message="查询失败" description={error} type="error" showIcon style={{ marginBottom: 16 }} />
      )}

      <Spin spinning={loading}>
        <Card style={{ background: 'var(--color-bg-white)' }}>
          <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between' }}>
            <Text type="secondary">共 {data?.total ?? 0} 条记录</Text>
          </div>
          <Table
            dataSource={data?.logs || []}
            columns={columns}
            rowKey={(record, index) => `audit-${index}`}
            pagination={false}
            expandable={{ expandedRowRender, defaultExpandedRowKeys: [] }}
            size="middle"
          />
          <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}>
            <Pagination
              current={Math.floor(offset / PAGE_SIZE) + 1}
              pageSize={PAGE_SIZE}
              total={data?.total ?? 0}
              onChange={(page) => fetchLogs((page - 1) * PAGE_SIZE)}
              showSizeChanger={false}
            />
          </div>
        </Card>
      </Spin>
    </div>
  )
}

export default AuditLogs
