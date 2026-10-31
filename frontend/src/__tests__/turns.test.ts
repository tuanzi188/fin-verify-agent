import { describe, it, expect } from 'vitest'
import { groupTurns, questionLabel } from '../utils/turns'
import type { Message } from '../hooks/useChat'

const user = (content: string): Message => ({ id: content, role: 'user', content, timestamp: 1 })
const assistant = (content: string, status: Message['status'] = 'answered'): Message => ({
  id: `a-${content}`,
  role: 'assistant',
  content,
  status,
  timestamp: 1,
})

describe('groupTurns', () => {
  it('pairs each question with its answer', () => {
    const turns = groupTurns([user('q1'), assistant('a1'), user('q2'), assistant('a2')])
    expect(turns).toHaveLength(2)
    expect(turns[0].question?.content).toBe('q1')
    expect(turns[0].answer?.content).toBe('a1')
    expect(turns[1].answer?.content).toBe('a2')
  })

  it('keeps a pending question without answer as its own turn', () => {
    const turns = groupTurns([user('q1'), assistant('a1'), user('q2')])
    expect(turns).toHaveLength(2)
    expect(turns[1].answer).toBeUndefined()
  })

  it('does not overwrite an assistant answer with a following error message', () => {
    const turns = groupTurns([user('q1'), assistant('a1'), assistant('出错', 'error')])
    expect(turns).toHaveLength(2)
    expect(turns[1].answer?.status).toBe('error')
  })

  it('handles a restored history whose first message is an answer', () => {
    const turns = groupTurns([assistant('a1')])
    expect(turns).toHaveLength(1)
    expect(turns[0].question).toBeUndefined()
  })
})

describe('questionLabel', () => {
  it('truncates long questions', () => {
    expect(questionLabel(user('一二三四五六七八九十'), 4)).toBe('一二三四…')
  })

  it('falls back when the question is missing', () => {
    expect(questionLabel(undefined)).toBe('未记录问题')
  })
})
