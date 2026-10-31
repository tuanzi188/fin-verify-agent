import React, { useState } from 'react'
import { Space, Typography } from 'antd'
import {
  ApartmentOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DownOutlined,
  UpOutlined,
} from '@ant-design/icons'
import type { AgentTraceStep } from '../api/client'
import { traceStepResult, traceStepTarget, traceToolLabel } from '../utils/trace'

const { Text } = Typography

/**
 * 用户版「查证过程」：只回答“查了哪份文档 / 哪张表 / 几次计算”。
 *
 * 工具名、参数、延迟、注入、错误码属于实验数据，只在 /lab 的评测产物里看，
 * 不进入普通查证页面。
 */

interface AgentTracePanelProps {
  trace?: AgentTraceStep[]
}

const AgentTracePanel: React.FC<AgentTracePanelProps> = ({ trace }) => {
  const [expanded, setExpanded] = useState(false)
  const steps = (trace || []).filter((step) => step.tool && step.tool !== '__final__')

  if (steps.length === 0) return null

  return (
    <div className="surface" style={{ marginTop: 10, padding: 'var(--space-2) var(--space-3)' }}>
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
          <Text strong style={{ fontSize: 12 }}>查证过程</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>{steps.length} 步</Text>
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
                : <CloseCircleOutlined style={{ color: 'var(--color-error)' }} />}
              <Text style={{ fontSize: 12 }}>{traceToolLabel(step.tool)}</Text>
              {traceStepTarget(step) && (
                <Text type="secondary" style={{ fontSize: 11 }}>{traceStepTarget(step)}</Text>
              )}
              <Text type="secondary" style={{ fontSize: 11 }}>{traceStepResult(step)}</Text>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default AgentTracePanel
