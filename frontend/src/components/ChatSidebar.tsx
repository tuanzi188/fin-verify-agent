import React from 'react'
import { Drawer, List, Spin, Button, Popconfirm, Tooltip, Tag } from 'antd'
import { Link } from 'react-router-dom'
import {
  PlusOutlined,
  DeleteOutlined,
  MessageOutlined,
  CloseOutlined,
  DatabaseOutlined,
} from '@ant-design/icons'
import { ConversationSummary, FinanceDocumentInfo, FinanceStats } from '../api/client'

interface ChatSidebarProps {
  visible: boolean
  mobile?: boolean
  conversations: ConversationSummary[]
  loading: boolean
  activeConversationId: string | undefined
  onNewConversation: () => void
  onSelectConversation: (id: string) => void
  onDeleteConversation: (id: string) => void
  onClose?: () => void
  formatDate: (ts: number) => string
  /** 查证材料：语料清单与覆盖范围。数据来自 /finance/documents、/finance/stats。 */
  materials?: FinanceDocumentInfo[]
  materialsLoading?: boolean
  coverage?: FinanceStats | null
  /** 当前这一轮引用到的文档 id；左栏只展示这些，不铺全量文档树。 */
  activeDocIds?: string[]
}

interface SidebarBodyProps extends ChatSidebarProps {
  compactHeader?: boolean
}

const SidebarBody: React.FC<SidebarBodyProps> = ({
  conversations,
  loading,
  activeConversationId,
  onNewConversation,
  onSelectConversation,
  onDeleteConversation,
  onClose,
  formatDate,
  materials = [],
  materialsLoading = false,
  coverage = null,
  activeDocIds = [],
  compactHeader = false,
}) => {
  const activeMaterials = activeDocIds
    .map((docId) => materials.find((doc) => doc.doc_id === docId))
    .filter((doc): doc is FinanceDocumentInfo => Boolean(doc))
  return (
    <>
      <div
        style={{
          padding: '12px 14px',
          borderBottom: '1px solid var(--color-border)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          flexShrink: 0,
        }}
      >
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-text)' }}>
            金融查证
          </div>
          <div style={{ fontSize: 10, color: 'var(--color-text-tertiary)', marginTop: 1 }}>
            选材料 · 提问 · 核对证据
          </div>
        </div>
        {onClose && (
          <Tooltip title="关闭">
            <Button
              type="text"
              size="small"
              icon={<CloseOutlined />}
              onClick={onClose}
              aria-label="关闭侧栏"
            />
          </Tooltip>
        )}
      </div>

      <div style={{ padding: '10px 12px 8px', flexShrink: 0 }}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          block
          onClick={() => {
            onNewConversation()
            if (compactHeader) onClose?.()
          }}
          className="btn-hover"
        >
          新建查证
        </Button>
      </div>

      {/* 语料覆盖：先回答“能查什么”，再回答“这一轮用了什么”。不做文档树。 */}
      <div
        style={{
          borderTop: 'var(--border-width) solid var(--color-border-light)',
          padding: 'var(--space-2) var(--space-3)',
          flexShrink: 0,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12, fontWeight: 600 }}>
            <DatabaseOutlined style={{ color: 'var(--color-text-tertiary)' }} />
            语料覆盖
          </div>
          <Link
            to="/documents"
            style={{ fontSize: 11, color: 'var(--color-primary)' }}
            onClick={() => {
              if (compactHeader) onClose?.()
            }}
          >
            全部材料
          </Link>
        </div>
        {coverage ? (
          <>
            <div className="meta-text" style={{ marginTop: 2 }}>
              {coverage.documents} 份文档 · {coverage.companies} 家公司 · {coverage.tables} 张表
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 6 }}>
              {coverage.company_names.slice(0, 4).map((company) => (
                <Tag key={company} style={{ margin: 0, fontSize: 10 }}>
                  {company}
                </Tag>
              ))}
              {coverage.periods.slice(0, 2).map((period) => (
                <Tag key={period} color="blue" style={{ margin: 0, fontSize: 10 }}>
                  {period}
                </Tag>
              ))}
            </div>
          </>
        ) : (
          <div className="meta-text" style={{ marginTop: 2 }}>语料未就绪</div>
        )}
      </div>

      {/* 当前材料：只列这一轮引用到的文档，避免左栏变成拥挤的文档树 */}
      <div
        style={{
          borderTop: 'var(--border-width) solid var(--color-border-light)',
          padding: 'var(--space-2) var(--space-3)',
          flexShrink: 0,
        }}
      >
        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-text-secondary)', marginBottom: 4 }}>
          当前材料
        </div>
        {materialsLoading ? (
          <div style={{ textAlign: 'center', padding: '6px 0' }}>
            <Spin size="small" />
          </div>
        ) : activeMaterials.length === 0 ? (
          <div className="meta-text">本轮还没有引用材料</div>
        ) : (
          activeMaterials.slice(0, 3).map((doc) => (
            <div key={doc.doc_id} title={doc.doc_id} style={{ padding: '3px 0' }}>
              <div
                style={{
                  fontSize: 12,
                  color: 'var(--color-text)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {doc.title || doc.doc_id}
              </div>
              <div className="meta-text">
                {[doc.company, doc.period, `${doc.tables} 表`].filter(Boolean).join(' · ')}
              </div>
            </div>
          ))
        )}
      </div>

      <div
        style={{
          padding: 'var(--space-2) var(--space-3) var(--space-1)',
          borderTop: 'var(--border-width) solid var(--color-border-light)',
          flexShrink: 0,
        }}
      >
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--color-text-secondary)' }}>最近查证</span>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: '0 10px 12px' }}>
        {loading ? (
          <div style={{ textAlign: 'center', padding: '40px 0' }}>
            <Spin size="small" />
          </div>
        ) : conversations.length === 0 ? (
          <div
            style={{
              textAlign: 'center',
              padding: '32px 16px',
              color: 'var(--color-text-tertiary)',
              fontSize: 13,
            }}
          >
            <MessageOutlined style={{ fontSize: 24, marginBottom: 8, display: 'block' }} />
            暂无查证记录
            <br />
            <span style={{ fontSize: 11 }}>从一个具体问题开始</span>
          </div>
        ) : (
          <List
            dataSource={conversations}
            renderItem={(conv) => (
              <div
                key={conv.conversation_id}
                className="session-row"
                onClick={() => {
                  onSelectConversation(conv.conversation_id)
                  if (compactHeader) onClose?.()
                }}
                style={{
                  padding: '10px 11px',
                  borderRadius: 'var(--radius-md)',
                  cursor: 'pointer',
                  marginBottom: 4,
                  background:
                    activeConversationId === conv.conversation_id
                      ? 'var(--color-primary-light)'
                      : 'transparent',
                  transition: 'all var(--transition-fast)',
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 8,
                }}
                onMouseEnter={(e) => {
                  if (activeConversationId !== conv.conversation_id) {
                    e.currentTarget.style.background = 'var(--color-bg)'
                  }
                }}
                onMouseLeave={(e) => {
                  if (activeConversationId !== conv.conversation_id) {
                    e.currentTarget.style.background = 'transparent'
                  }
                }}
              >
                <MessageOutlined
                  style={{
                    color: 'var(--color-text-tertiary)',
                    marginTop: 2,
                    flexShrink: 0,
                    fontSize: 13,
                  }}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 13,
                      color: 'var(--color-text)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      fontWeight: activeConversationId === conv.conversation_id ? 500 : 400,
                    }}
                  >
                    {conv.preview || '新查证'}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--color-text-tertiary)', marginTop: 2 }}>
                    {formatDate(conv.last_updated)} · {conv.turn_count} 轮
                  </div>
                </div>
                <Popconfirm
                  title="确定删除此会话？"
                  onConfirm={(e) => {
                    e?.stopPropagation()
                    onDeleteConversation(conv.conversation_id)
                  }}
                  onCancel={(e) => e?.stopPropagation()}
                  okText="删除"
                  cancelText="取消"
                >
                  <Button
                    type="text"
                    size="small"
                    className="session-delete"
                    icon={<DeleteOutlined style={{ fontSize: 12 }} />}
                    aria-label="删除会话"
                    style={{ color: 'var(--color-text-tertiary)', flexShrink: 0 }}
                    onClick={(e) => e.stopPropagation()}
                  />
                </Popconfirm>
              </div>
            )}
          />
        )}
      </div>
    </>
  )
}

const ChatSidebar: React.FC<ChatSidebarProps> = (props) => {
  if (props.mobile) {
    return (
      <Drawer
        placement="left"
        width={280}
        open={props.visible}
        onClose={props.onClose}
        closable={false}
        styles={{ body: { padding: 0, display: 'flex', flexDirection: 'column' } }}
      >
        <SidebarBody {...props} compactHeader onClose={props.onClose} />
      </Drawer>
    )
  }

  return (
    <aside
      className={props.visible ? undefined : 'sidebar-collapsed-mobile'}
      style={{
        width: props.visible ? 260 : 0,
        minWidth: props.visible ? 260 : 0,
        background: 'var(--color-bg-white)',
        borderRight: props.visible ? '1px solid var(--color-border)' : 'none',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        transition: 'width var(--transition-normal), min-width var(--transition-normal)',
        position: 'relative',
        flexShrink: 0,
      }}
    >
      {/* 桌面端只用 Header 的折叠按钮，避免侧栏里再放一个重复的关闭按钮。 */}
      <SidebarBody {...props} onClose={undefined} />
    </aside>
  )
}

export default ChatSidebar
