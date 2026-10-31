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
  status?: TurnStatus
  timestampLabel?: string
}

/** 当前查证的问题头：把“问什么”放在结论上方，而不是藏在气泡里。 */
export const TurnQuestion: React.FC<TurnQuestionProps> = ({
  turn,
  index,
  status,
  timestampLabel,
}) => {
  const question = turn.question?.content?.trim()
  const meta = status ? STATUS_META[status] : undefined
  const strategy = turn.answer?.finance?.strategy
  return (
    <div className="question-head">
      <div className="question-meta">
        <Tag style={{ margin: 0, fontSize: 11 }}>第 {index + 1} 次查证</Tag>
        {meta && (
          <Tag color={meta.color} style={{ margin: 0, fontSize: 11 }}>
            {meta.label}
          </Tag>
        )}
        {strategy && (
          <Text className="question-meta-text">
            {strategy === 'structured_rag' ? '结构检索' : '多步查证'}
          </Text>
        )}
        {timestampLabel && <Text className="question-meta-text">{timestampLabel}</Text>}
      </div>
      <div className="question-title">{question || '（未记录问题）'}</div>
    </div>
  )
}
