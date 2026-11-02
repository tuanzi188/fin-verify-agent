import React from 'react'
import { Tag, Tooltip, Typography } from 'antd'
import { STATUS_META, TONE_VAR, type TurnStatus } from '../utils/status'
import { questionLabel, TurnGroup } from '../utils/turns'

const { Text } = Typography

interface TurnStripProps {
  turns: TurnGroup[]
  statuses: (TurnStatus | undefined)[]
  activeIndex: number
  onSelect: (index: number) => void
}

/**
 * 查证轮次条：一轮一次问题，替代聊天记录。
 * 结论区长，切换用这个条，不在主列堆历史气泡。
 */
export const TurnStrip: React.FC<TurnStripProps> = ({ turns, statuses, activeIndex, onSelect }) => {
  if (turns.length <= 1) return null
  return (
    <div className="turn-strip">
      <span className="turn-strip-label">查证记录</span>
      {turns.map((turn, index) => {
        const status = statuses[index]
        const meta = status ? STATUS_META[status] : undefined
        return (
          <Tooltip key={index} title={turn.question?.content || '未记录问题'}>
            <button
              type="button"
              onClick={() => onSelect(index)}
              className={`turn-chip${index === activeIndex ? ' is-active' : ''}`}
            >
              <span className="turn-chip-index">{index + 1}</span>
              <span className="turn-chip-label">{questionLabel(turn.question, 18)}</span>
              {meta && (
                <span
                  className="status-dot"
                  aria-label={meta.label}
                  title={meta.label}
                  style={{ background: TONE_VAR[meta.tone] }}
                />
              )}
            </button>
          </Tooltip>
        )
      })}
    </div>
  )
}

interface TurnQuestionProps {
  turn: TurnGroup
  index: number
  timestampLabel?: string
}

/** 查证范围：只取证据里明确给过的公司/期间，不猜指标。 */
interface ScopeFinance {
  citations?: Array<{ evidence?: { company?: string; period?: string } }>
}

function scopeLabel(finance?: ScopeFinance): string {
  const evidence = finance?.citations?.[0]?.evidence
  if (!evidence) return ''
  return [evidence.company, evidence.period].filter(Boolean).join(' · ')
}

/** 当前查证的问题头：把“问什么”放在结论上方，而不是藏在气泡里。 */
export const TurnQuestion: React.FC<TurnQuestionProps> = ({
  turn,
  index,
  timestampLabel,
}) => {
  const question = turn.question?.content?.trim()
  const scope = scopeLabel(turn.answer?.finance)
  return (
    <div className="question-head">
      <div className="question-meta">
        <Text className="question-meta-text">第 {index + 1} 次查证</Text>
        {timestampLabel && <Text className="question-meta-text">{timestampLabel}</Text>}
      </div>
      <div className="question-title">{question || '（未记录问题）'}</div>
      {scope && <div className="scope-line">{scope}</div>}
    </div>
  )
}
