import React, { useState } from 'react'
import { Space, Tag, Typography } from 'antd'
import {
  ApartmentOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DownOutlined,
  UpOutlined,
} from '@ant-design/icons'
import type { AgentTraceStep } from '../api/client'

const { Text } = Typography

interface AgentTracePanelProps {
  trace?: AgentTraceStep[]
}

/**
 * Agent 执行轨迹。默认折叠，点标题栏展开。
 * 仿 `ReasoningSection` 的手写折叠，不引 antd Collapse。
 */
const AgentTracePanel: React.FC<AgentTracePanelProps> = ({ trace }) => {
  const [expanded, setExpanded] = useState(false)
  const steps = (trace || []).filter((step) => step.tool && step.tool !== '__final__')

  if (steps.length === 0) return null

  const totalMs = steps.reduce((sum, step) => sum + (step.latency_ms || 0), 0)

  return (
    <div
      style={{
        marginTop: 10,
        border: '1px solid var(--color-border)',
        borderRadius: 'var(--radius-md)',
        background: 'var(--color-bg)',
        padding: '8px 10px',
      }}
    >
      <div
        role="button"
        tabIndex={0}
        onClick={() => setExpanded((prev) => !prev)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault()
            setExpanded((prev) => !prev)
          }
        }}
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          cursor: 'pointer',
          userSelect: 'none',
        }}
      >
        <Space size={6}>
          <ApartmentOutlined style={{ color: 'var(--color-primary)' }} />
          <Text strong style={{ fontSize: 12 }}>执行轨迹</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {steps.length} 步 · {totalMs} ms
          </Text>
        </Space>
        {expanded
          ? <UpOutlined style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }} />
          : <DownOutlined style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }} />}
      </div>

      {expanded && (
        <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {steps.map((step) => (
            <div
              key={`${step.step}-${step.tool}`}
              style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, flexWrap: 'wrap' }}
            >
              <Text type="secondary" style={{ width: 16, fontSize: 11 }}>{step.step}</Text>
              {step.ok
                ? <CheckCircleOutlined style={{ color: 'var(--color-success)' }} />
                : <CloseCircleOutlined style={{ color: '#ff4d4f' }} />}
              <Text code style={{ fontSize: 11 }}>{step.tool}</Text>
              <Text type="secondary">{step.result_count ?? 0} 条</Text>
              <Text type="secondary">{step.latency_ms ?? 0} ms</Text>
              {step.injected && (
                <Tag color="orange" style={{ margin: 0, fontSize: 11 }}>注入: {step.injected}</Tag>
              )}
              {step.error_kind && (
                <Tag color="red" style={{ margin: 0, fontSize: 11 }}>{step.error_kind}</Tag>
              )}
              {step.args && typeof step.args.query === 'string' && (
                <Text type="secondary" style={{ fontSize: 11 }}>查询: {step.args.query}</Text>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default AgentTracePanel
