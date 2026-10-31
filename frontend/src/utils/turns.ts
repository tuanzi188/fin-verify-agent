import type { Message } from '../hooks/useChat'

/** 一次查证 = 一个问题 + 一条回答。错误消息也归到当前问题下，避免拆成单独一轮。 */
export interface TurnGroup {
  question?: Message
  answer?: Message
}

/**
 * 把消息流按「问—答」配对。
 * 连续两条 assistant（如错误提示紧跟部分回答）时，后一条单独成轮，不覆盖前一条。
 */
export function groupTurns(messages: Message[]): TurnGroup[] {
  const turns: TurnGroup[] = []
  for (const message of messages) {
    if (message.role === 'user') {
      turns.push({ question: message })
      continue
    }
    const current = turns[turns.length - 1]
    if (current && !current.answer) {
      current.answer = message
    } else {
      turns.push({ answer: message })
    }
  }
  return turns
}

/** 用第一条问题做侧栏/轮次条标题；过长时截断。 */
export function questionLabel(message: Message | undefined, limit = 28): string {
  const text = (message?.content || '').replace(/\s+/g, ' ').trim()
  if (!text) return '未记录问题'
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}
