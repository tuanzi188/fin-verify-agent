/**
 * 查证状态的唯一口径。
 *
 * 后端只产出 answered / abstained / clarified / error 四种；`needs_review`
 * 是前端根据“证据还能不能核验”派生的第五种状态（语料版本变化、结论无引用、
 * 回查 404）。任何组件都从这里取标签和颜色，避免各写一套。
 */
export type TurnStatus = 'answered' | 'abstained' | 'clarified' | 'needs_review' | 'error'

export type StatusTone = 'success' | 'warning' | 'info' | 'review' | 'error'

export interface StatusMeta {
  label: string
  tone: StatusTone
  /** antd Tag 的颜色，仅用于 antd 组件；自定义样式用 tone。 */
  color: string
}

export const STATUS_META: Record<TurnStatus, StatusMeta> = {
  answered: { label: '已回答', tone: 'success', color: 'green' },
  abstained: { label: '资料不足', tone: 'warning', color: 'orange' },
  clarified: { label: '需澄清', tone: 'info', color: 'blue' },
  needs_review: { label: '需人工复核', tone: 'review', color: 'orange' },
  error: { label: '执行失败', tone: 'error', color: 'red' },
}

const BACKEND_STATUSES: TurnStatus[] = ['answered', 'abstained', 'clarified', 'error']

export function normalizeStatus(raw?: string | null): TurnStatus {
  return BACKEND_STATUSES.includes(raw as TurnStatus) ? (raw as TurnStatus) : 'error'
}

/** tone → CSS 变量。自定义组件统一用这个，不直接写色值。 */
export const TONE_VAR: Record<StatusTone, string> = {
  success: 'var(--color-success)',
  warning: 'var(--color-warning)',
  info: 'var(--color-primary)',
  review: 'var(--color-review)',
  error: 'var(--color-error)',
}

export interface DeriveStatusOptions {
  /** 会话语料指纹与当前语料不一致。 */
  corpusChanged?: boolean
  /** 有结构化要点。 */
  hasClaims?: boolean
  /** 有可核验证据。 */
  hasCitations?: boolean
}

/** 后端状态 + 证据可核验性 → 最终展示状态。 */
export function deriveStatus(raw: string | undefined, options: DeriveStatusOptions = {}): TurnStatus {
  const base = normalizeStatus(raw)
  if (base === 'error' || base === 'abstained' || base === 'clarified') return base
  if (options.corpusChanged) return 'needs_review'
  if (options.hasClaims && !options.hasCitations) return 'needs_review'
  return base
}
