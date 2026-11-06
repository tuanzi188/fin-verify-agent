import React from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { MarketBrief } from '../api/client'

/**
 * 联网综述卡片。
 *
 * 与查证结论卡的区别是**整块都要带"未核实"标记**：这里的每一句都是别人说的，
 * 没有页码、没有单元格，不能像 `[n]` 那样点开原文。免责由服务端追加，这里只负责展示。
 */

const muted: React.CSSProperties = {
  color: 'var(--color-text-secondary)',
  fontSize: 12,
}

/** 丢来源的理由要说人话：用户据此判断"是不是被故意挡掉了"。 */
const SKIP_REASONS: Record<string, string> = {
  not_whitelisted: '不在白名单站点',
  blocked_domain: '黑名单站点',
  missing_published_at: '缺少发布日期',
  duplicate: '重复链接',
  missing_url: '无效链接',
  official_original: '官方原件（转入库候选）',
  bad_title: '标题无信息量',
  domain_cap: '同域名超出条数上限',
}

const SectionTitle: React.FC<{ children: React.ReactNode; color?: string }> = ({
  children,
  color,
}) => (  <div
    style={{
      fontSize: 12,
      fontWeight: 600,
      letterSpacing: 0.2,
      color: color || 'var(--color-text-secondary)',
      margin: '14px 0 6px',
    }}
  >
    {children}
  </div>
)

const Items: React.FC<{ items?: string[] }> = ({ items }) => {
  if (!items || items.length === 0) return null
  return (
    <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.75 }}>
      {items.map((item, index) => (
        <li key={index} style={{ marginBottom: 4 }}>
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              p: ({ children }) => <span>{children}</span>,
            }}
          >
            {item}
          </ReactMarkdown>
        </li>
      ))}
    </ul>
  )
}

const MarketBriefCard: React.FC<{ brief: MarketBrief }> = ({ brief }) => {
  const sources = brief.sources || []
  const points = (brief.sections && brief.sections['分项']) || []

  return (
    <div
      style={{
        borderLeft: '3px solid #d48806',
        background: 'var(--color-bg)',
        borderRadius: 'var(--radius-md)',
        padding: '10px 14px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: '#ad6800' }}>
          公开信息综述
        </span>
        <span
          style={{
            fontSize: 11,
            color: '#ad6800',
            border: '1px solid #ffd591',
            background: '#fff7e6',
            borderRadius: 3,
            padding: '0 4px',
          }}
        >
          未核实
        </span>
        <span style={muted}>
          {brief.as_of ? `截至 ${brief.as_of.slice(0, 16).replace('T', ' ')}` : ''}
          {sources.length ? ` · 参考 ${sources.length} 篇` : ''}
        </span>
      </div>

      {brief.summary ? (
        <div
          className="markdown-body"
          style={{ fontSize: 14, lineHeight: 1.8, marginTop: 8, color: 'var(--color-text)' }}
        >
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{brief.summary}</ReactMarkdown>
        </div>
      ) : null}

      {points.length > 0 && (
        <>
          <SectionTitle>分项</SectionTitle>
          <Items items={points} />
        </>
      )}

      {brief.misreadings && brief.misreadings.length > 0 && (
        <>
          <SectionTitle color="#a8071a">常见误读</SectionTitle>
          <Items items={brief.misreadings} />
        </>
      )}

      {brief.divergences && brief.divergences.length > 0 && (
        <>
          <SectionTitle>分歧</SectionTitle>
          <Items items={brief.divergences} />
        </>
      )}

      {brief.gaps && brief.gaps.length > 0 && (
        <>
          <SectionTitle>资料缺口</SectionTitle>
          <Items items={brief.gaps} />
        </>
      )}

      {brief.warnings && brief.warnings.length > 0 && (
        <div
          style={{
            marginTop: 10,
            padding: '6px 8px',
            borderRadius: 4,
            background: '#fff1f0',
            border: '1px solid #ffa39e',
            fontSize: 12,
            color: '#a8071a',
            lineHeight: 1.6,
          }}
        >
          {brief.warnings.map((warning, index) => (
            <div key={index}>· {warning}</div>
          ))}
        </div>
      )}

      {sources.length > 0 && (
        <details style={{ marginTop: 10 }}>
          <summary style={{ cursor: 'pointer', ...muted }}>
            参考资料（{sources.length}）
          </summary>
          <ol style={{ margin: '6px 0 0', paddingLeft: 20, fontSize: 12, lineHeight: 1.7 }}>
            {sources.map((source) => (
              <li key={source.url} style={{ marginBottom: 6 }}>
                <span style={{ color: source.tier === 1 ? '#ad6800' : 'var(--color-text-secondary)' }}>
                  [{source.tier === 1 ? '官方' : source.tier === 2 ? '媒体' : '门户'}]
                </span>{' '}
                <a href={source.url} target="_blank" rel="noopener noreferrer">
                  {source.title || source.url}
                </a>
                <span style={muted}>
                  {' '}
                  · {source.label}
                  {source.published_at ? ` · ${source.published_at.slice(0, 10)}` : ' · 日期未知'}
                </span>
                {/* 摘要原文是"这句话有没有来源"的唯一凭据，不能只给标题 */}
                {source.snippet ? (
                  <div style={{ color: 'var(--color-text-secondary)', marginTop: 2 }}>
                    {source.snippet}
                  </div>
                ) : null}
              </li>
            ))}
          </ol>
          {brief.queries && brief.queries.length > 0 && (
            <div style={{ ...muted, marginTop: 6 }}>
              实际检索词：{brief.queries.map((query) => `「${query}」`).join('、')}
            </div>
          )}
        </details>
      )}

      {brief.ingest_candidates && brief.ingest_candidates.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary style={{ cursor: 'pointer', ...muted }}>
            发现的官方 PDF（{brief.ingest_candidates.length}，入库后才可作为证据）
          </summary>
          <ul style={{ margin: '6px 0 0', paddingLeft: 18, fontSize: 12, lineHeight: 1.7 }}>
            {brief.ingest_candidates.map((item) => (
              <li key={item.url} style={{ marginBottom: 3 }}>
                <a href={item.url} target="_blank" rel="noopener noreferrer">
                  {item.title || item.url}
                </a>
                <span style={muted}> · {item.domain}</span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {brief.skipped_sources && brief.skipped_sources.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary style={{ cursor: 'pointer', ...muted }}>
            未采用的来源（{brief.skipped_sources.length}）
          </summary>
          <ul style={{ margin: '6px 0 0', paddingLeft: 18, fontSize: 12, lineHeight: 1.7 }}>
            {brief.skipped_sources.map((item) => (
              <li key={item.url} style={{ marginBottom: 3 }}>
                {item.url}
                <span style={muted}>
                  {' '}
                  · {SKIP_REASONS[item.reason] || item.reason || '未知原因'}
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}

      {brief.disclaimer ? (
        <div style={{ ...muted, marginTop: 10, lineHeight: 1.6 }}>{brief.disclaimer}</div>
      ) : null}
    </div>
  )
}

export default MarketBriefCard
