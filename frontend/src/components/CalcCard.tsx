import React from 'react'
import { Tag, Typography } from 'antd'
import { CalculatorOutlined } from '@ant-design/icons'
import type { CalcRecord } from '../api/client'

const { Text } = Typography

export interface CalcCardProps {
  calc: CalcRecord
  index: number
  /** 传入即可点击：打开证据检查器并定位到这条计算。 */
  onClick?: () => void
  /** 标题右侧的提示语，例如「点击核对输入」。 */
  hint?: string
  /** 底部附加操作，例如「只看这一条」。 */
  footer?: React.ReactNode
  /** note 前缀，结论卡里用「口径：」。 */
  notePrefix?: string
  /** 结论卡用 success 突出凭证；证据面板里用 neutral 不抢视线。 */
  tone?: 'neutral' | 'success'
  /** 紧凑模式：输入压成一行、结果字号降一级。 */
  compact?: boolean
}

const TONE: Record<'neutral' | 'success', { border: string; background: string }> = {
  neutral: { border: 'var(--color-border)', background: 'var(--color-bg)' },
  success: { border: 'var(--color-success-border)', background: 'var(--color-success-soft)' },
}

const CalcCard: React.FC<CalcCardProps> = ({
  calc,
  index,
  onClick,
  hint,
  footer,
  notePrefix,
  tone = 'neutral',
  compact = false,
}) => {
  const inputs = Object.entries(calc.inputs || {})
  const interactive = Boolean(onClick)
  const surface = TONE[tone]

  return (
    <div
      role={interactive ? 'button' : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={onClick}
      onKeyDown={
        interactive
          ? (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                onClick?.()
              }
            }
          : undefined
      }
      style={{
        marginTop: compact ? 8 : undefined,
        padding: '10px 12px',
        borderRadius: 'var(--radius-md)',
        border: `1px solid ${surface.border}`,
        background: surface.background,
        cursor: interactive ? 'pointer' : undefined,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
        <CalculatorOutlined style={{ color: 'var(--color-success)' }} />
        <Text strong style={{ fontSize: 13 }}>计算 {index + 1}</Text>
        {calc.unit && (
          <Tag color="green" style={{ margin: 0, fontSize: compact ? 10 : undefined }}>
            {calc.unit}
          </Tag>
        )}
        {hint && (
          <Text type="secondary" style={{ fontSize: 11 }}>
            {hint}
          </Text>
        )}
      </div>

      <div
        style={{
          fontFamily: 'Consolas, Monaco, monospace',
          fontSize: 13,
          lineHeight: 1.7,
          wordBreak: 'break-all',
          color: 'var(--color-text)',
        }}
      >
        {calc.formula}
      </div>

      {compact ? (
        inputs.length > 0 && (
          <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginTop: 4 }}>
            {inputs.map(([key, value]) => `${key}=${String(value)}`).join(' · ')}
          </div>
        )
      ) : (
        <div style={{ marginTop: 6 }}>
          {inputs.map(([key, value]) => (
            <div key={key} style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
              {key} = {String(value)}
              {calc.input_sources?.[key] && (
                <span> · 来源 [{calc.input_sources[key].marker}] 行 {calc.input_sources[key].row} 列 {calc.input_sources[key].column}</span>
              )}
            </div>
          ))}
        </div>
      )}

      <div
        style={{
          marginTop: compact ? 4 : 6,
          fontSize: compact ? 13 : 14,
          fontWeight: compact ? 600 : 700,
          color: 'var(--color-success)',
        }}
      >
        = {String(calc.result)}
        {calc.unit ? ` ${calc.unit}` : ''}
      </div>

      {calc.result_scale && calc.result_scale !== '1' && (
        <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginTop: 4 }}>
          展示结果 = 公式原始结果 × {calc.result_scale}
        </div>
      )}
      {calc.note && (
        <div
          className={compact ? 'meta-text' : undefined}
          style={
            compact
              ? { marginTop: 2 }
              : { marginTop: 6, fontSize: 12, color: 'var(--color-text-tertiary)' }
          }
        >
          {notePrefix}
          {calc.note}
        </div>
      )}

      {footer}
    </div>
  )
}

export default CalcCard
