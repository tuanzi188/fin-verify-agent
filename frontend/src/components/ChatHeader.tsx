import React, { useState } from 'react'
import { Button, Dropdown, Badge, Tooltip, Modal, Typography, Tag } from 'antd'
import {
  UserOutlined,
  QuestionCircleOutlined,
  BellOutlined,
  SettingOutlined,
  LogoutOutlined,
  SunOutlined,
  MoonOutlined,
  ReloadOutlined,
  ExperimentOutlined,
  SafetyOutlined,
  FolderOpenOutlined,
  FileSearchOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MoreOutlined,
} from '@ant-design/icons'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { ConnectionState } from '../types/connection'
import { useUser } from '../contexts/UserContext'
import UserSettingsDrawer from './UserSettingsDrawer'
import { FinanceStats } from '../api/client'

const { Text } = Typography

interface ChatHeaderProps {
  isDark: boolean
  stats: FinanceStats | null
  connectionState: ConnectionState
  mobile?: boolean
  compact?: boolean
  sidebarVisible?: boolean
  onToggleSidebar?: () => void
  onToggleTheme: () => void
  onReconnect: () => void
}

const ChatHeader: React.FC<ChatHeaderProps> = ({
  isDark,
  stats,
  connectionState,
  mobile = false,
  compact = false,
  sidebarVisible,
  onToggleSidebar,
  onToggleTheme,
  onReconnect,
}) => {
  const [notificationCount] = useState(0)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const { userId, isAdmin, logout } = useUser()
  const location = useLocation()
  const navigate = useNavigate()

  const userMenuItems = [
    { key: 'settings', icon: <SettingOutlined />, label: '个人设置' },
    { type: 'divider' as const },
    { key: 'logout', icon: <LogoutOutlined />, label: '退出登录', danger: true },
  ]

  const handleUserMenuClick = ({ key }: { key: string }) => {
    if (key === 'logout') logout()
    else if (key === 'settings') setSettingsOpen(true)
  }

  const navItems = [
    { key: '/', label: '查证', icon: <FileSearchOutlined />, path: '/' },
    { key: '/documents', label: '语料', icon: <FolderOpenOutlined />, path: '/documents' },
    { key: '/lab', label: '实验', icon: <ExperimentOutlined />, path: '/lab' },
    { key: '/audit', label: '审计', icon: <SafetyOutlined />, path: '/audit', adminOnly: true },
  ]

  const visibleNav = navItems.filter((item) => !item.adminOnly || isAdmin)

  const shortcutContent = (
    <div style={{ maxWidth: 300 }}>
      <div style={{ marginBottom: 8 }}>
        <Text strong>快捷键</Text>
      </div>
      <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', lineHeight: 2 }}>
        <div>Enter 发送问题</div>
        <div>Shift + Enter 换行</div>
        <div>Esc 停止生成 / 清空输入</div>
        <div>Ctrl + K 新建查证</div>
      </div>
      <div style={{ marginTop: 10, marginBottom: 4 }}>
        <Text strong>证据检查器</Text>
      </div>
      <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', lineHeight: 1.8 }}>
        点击回答里的引用胶囊，右侧会定位到章节、表格行列或计算记录。
        页码是 PDF 解析页码，不提供原版截图高亮。
      </div>
    </div>
  )

  const moreMenuItems = [
    { key: 'help', icon: <QuestionCircleOutlined />, label: '帮助与快捷键' },
    { key: 'theme', icon: isDark ? <SunOutlined /> : <MoonOutlined />, label: isDark ? '切换亮色' : '切换暗色' },
    ...(visibleNav
      .filter((item) => item.path !== '/')
      .map((item) => ({ key: `nav:${item.path}`, icon: item.icon, label: item.label }))),
  ]

  const handleMoreMenuClick = ({ key }: { key: string }) => {
    if (key === 'help') setHelpOpen(true)
    else if (key === 'theme') onToggleTheme()
    else if (key.startsWith('nav:')) navigate(key.slice(4))
  }

  const baseBadgeStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    padding: compact ? '4px 6px' : '5px 10px',
    borderRadius: 'var(--radius-md)',
    fontSize: 12,
    fontWeight: 500,
    transition: 'all var(--transition-fast)',
    cursor: 'default',
    userSelect: 'none',
  }

  const renderConnectionBadge = () => {
    if (connectionState === 'connecting') {
      return (
        <Tooltip title="正在检查后端服务...">
          <div style={{ ...baseBadgeStyle, background: 'var(--color-primary-light)', color: 'var(--color-primary)' }}>
            <div style={{ position: 'relative', width: 14, height: 14, flexShrink: 0 }}>
              <svg width="14" height="14" viewBox="0 0 14 14" className="connection-ring">
                <circle
                  cx="7" cy="7" r="5.5"
                  fill="none"
                  stroke="var(--color-primary)"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeDasharray="26"
                  strokeDashoffset="8"
                />
              </svg>
            </div>
            {!compact && <span>连接中</span>}
          </div>
        </Tooltip>
      )
    }

    if (connectionState === 'connected') {
      return (
        <Tooltip title="后端服务已连接">
          <div style={{
            ...baseBadgeStyle,
            background: 'rgba(82, 196, 26, 0.12)',
            color: 'var(--color-success)',
            border: '1px solid rgba(82, 196, 26, 0.25)',
          }}>
            <div style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--color-success)', flexShrink: 0 }} />
            {!compact && <span>已连接</span>}
          </div>
        </Tooltip>
      )
    }

    return (
      <Tooltip title="后端服务不可用，点击重试">
        <div
          onClick={onReconnect}
          style={{
            ...baseBadgeStyle,
            background: 'rgba(255, 77, 79, 0.12)',
            color: 'var(--color-error)',
            border: '1px solid rgba(255, 77, 79, 0.25)',
            cursor: 'pointer',
          }}
        >
          <ReloadOutlined style={{ fontSize: 11, flexShrink: 0 }} />
          {!compact && <span>服务重连</span>}
        </div>
      </Tooltip>
    )
  }

  return (
    <div
      className="chat-header"
      style={{
        padding: mobile ? '10px 12px' : '10px 20px',
        background: 'var(--color-bg-white)',
        borderBottom: '1px solid var(--color-border)',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        gap: 8,
        transition: 'background var(--transition-normal)',
        minWidth: 0,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, flex: 1 }}>
        {onToggleSidebar && (
          <Tooltip title={sidebarVisible ? '收起最近查证' : '展开最近查证'}>
            <Button
              type="text"
              size="small"
              icon={sidebarVisible ? <MenuFoldOutlined /> : <MenuUnfoldOutlined />}
              onClick={onToggleSidebar}
              aria-label="切换侧栏"
              style={{
                color: 'var(--color-text-secondary)',
                width: 32,
                height: 32,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            />
          </Tooltip>
        )}

        <div
          style={{
            width: mobile ? 30 : 34,
            height: mobile ? 30 : 34,
            borderRadius: 'var(--radius-md)',
            background: 'linear-gradient(135deg, #0f3b66 0%, #145a8a 100%)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: '#fff', fontSize: mobile ? 15 : 17, fontWeight: 700, flexShrink: 0,
          }}
        >
          F
        </div>
        <div style={{ lineHeight: 1.25, minWidth: 0 }}>
          <div
            style={{
              fontSize: mobile ? 14 : 15,
              fontWeight: 700,
              color: 'var(--color-text)',
              whiteSpace: 'nowrap',
            }}
          >
            金融查证
          </div>
          <div
            style={{
              fontSize: 10,
              color: 'var(--color-text-tertiary)',
              letterSpacing: 0.4,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            证据工作台{' '}
            <span style={{ marginLeft: 2, color: 'var(--color-text-secondary)' }}>
              · {stats ? `${stats.documents} 份文档` : '语料待加载'}
            </span>
          </div>
        </div>

        {!compact && (
          <>
            <div style={{ width: 1, height: 22, background: 'var(--color-border)', margin: '0 6px' }} />
            <nav style={{ display: 'flex', alignItems: 'center', gap: 2 }}>
              {visibleNav.map((item) => {
                const active = location.pathname === item.path
                return (
                  <Link
                    key={item.key}
                    to={item.path}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 5,
                      padding: '6px 9px',
                      borderRadius: 'var(--radius-md)',
                      textDecoration: 'none',
                      fontSize: 13,
                      fontWeight: active ? 500 : 400,
                      color: active ? 'var(--color-primary)' : 'var(--color-text)',
                      background: active ? 'var(--color-primary-light)' : 'transparent',
                      transition: 'all var(--transition-fast)',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {item.icon}
                    {item.label}
                  </Link>
                )
              })}
            </nav>
          </>
        )}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
        {renderConnectionBadge()}

        {!compact && (
          <>
            <Tooltip title="帮助与快捷键">
              <Button
                type="text"
                size="small"
                icon={<QuestionCircleOutlined style={{ fontSize: 16 }} />}
                onClick={() => setHelpOpen(true)}
                className="btn-hover"
              />
            </Tooltip>
            <Tooltip title={isDark ? '切换亮色模式' : '切换暗色模式'}>
              <Button
                type="text" size="small"
                icon={isDark ? <SunOutlined style={{ fontSize: 16 }} /> : <MoonOutlined style={{ fontSize: 16 }} />}
                onClick={onToggleTheme}
                className="btn-hover"
              />
            </Tooltip>
          </>
        )}

        {compact && (
          <Dropdown
            menu={{ items: moreMenuItems, onClick: handleMoreMenuClick }}
            trigger={['click']}
            placement="bottomRight"
          >
            <Button type="text" size="small" icon={<MoreOutlined style={{ fontSize: 18 }} />} aria-label="更多操作" />
          </Dropdown>
        )}

        {!compact && (
          <Tooltip title="消息通知">
            <Badge count={notificationCount} size="small" offset={[-2, 2]}>
              <Button type="text" size="small" icon={<BellOutlined style={{ fontSize: 16 }} />} className="btn-hover" />
            </Badge>
          </Tooltip>
        )}

        {!mobile && isAdmin && (
          <Tag color="red" style={{ marginRight: 2 }}>
            管理员
          </Tag>
        )}

        <Dropdown
          menu={{ items: userMenuItems, onClick: handleUserMenuClick }}
          trigger={['click']}
          placement="bottomRight"
        >
          <Button
            type="text" size="small"
            icon={<UserOutlined />}
            className="btn-hover"
            style={{ display: 'flex', alignItems: 'center', gap: 4 }}
          >
            {!mobile && (
              <span style={{ fontSize: 13, maxWidth: 80, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {userId || 'anonymous'}
              </span>
            )}
          </Button>
        </Dropdown>
      </div>

      <UserSettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <Modal
        title="帮助与快捷键"
        open={helpOpen}
        onCancel={() => setHelpOpen(false)}
        footer={null}
        width={420}
      >
        {shortcutContent}
      </Modal>
    </div>
  )
}

export default ChatHeader
