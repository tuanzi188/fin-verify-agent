import type { AgentTraceStep } from '../api/client'

/** 用户可读的查证动作名；实验用的工具名只在 /lab 产物里出现。 */
export const TOOL_LABELS: Record<string, string> = {
  search_blocks: '检索结构块',
  search_sections: '检索章节',
  read_block: '读取段落',
  read_section: '读取章节',
  get_rows: '读取表格行',
  get_table: '读取表格',
  get_cell: '读取单元格',
  list_documents: '列出文档',
  calculate: '计算',
  __final__: '生成结论',
}

export function shortTraceId(value?: string | null): string {
  if (!value) return ''
  const parts = value.split('#')
  return parts[parts.length - 1] || value
}

export function traceToolLabel(tool: string): string {
  return TOOL_LABELS[tool] || '查证'
}

export function traceStepTarget(step: AgentTraceStep): string {
  if (step.table_id) return `表 ${shortTraceId(step.table_id)}`
  const query = step.args && typeof step.args.query === 'string' ? step.args.query : ''
  if (query) return `查询“${query.length > 18 ? `${query.slice(0, 18)}…` : query}”`
  const firstId = step.result_ids?.[0]
  return firstId ? shortTraceId(firstId) : ''
}

export function traceStepResult(step: AgentTraceStep): string {
  if (!step.ok) return '没有拿到可用证据'
  const count = step.new_result_count ?? step.result_count ?? 0
  if (!count) return '完成'
  return step.new_result_count !== undefined ? `新增 ${count} 条` : `${count} 条结果`
}
