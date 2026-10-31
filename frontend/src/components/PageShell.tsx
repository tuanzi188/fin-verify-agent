import React, { useState } from 'react'
import { Button, Dropdown, Tooltip, Tag } from 'antd'
import {
  UserOutlined,
  SettingOutlined,
  SunOutlined,
  MoonOutlined,
  LogoutOutlined,
  FileSearchOutlined,
  ExperimentOutlined,
  FolderOpenOutlined,
  SafetyOutlined,
} from '@ant-design/icons'
import { Link, useLocation } from 'react-router-dom'
import { useUser } from '../contexts/UserContext'
import UserSettingsDrawer from './UserSettingsDrawer'
import { useMediaQuery } from '../hooks/useMediaQuery'

interface PageShellProps {
  children: React.ReactNode
  isDark?: boolean
  onToggleTheme?: () => void
}

const PageShell: React.FC<PageShellProps> = ({ children, isDark, onToggleTheme }) => {
  const location = useLocation()
  const { userId, isAdmin, logout } = useUser()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const compact = useMediaQuery('(max-width: 900px)')

  const navItems = [
    { key: '/', label: '查证', icon: <FileSearchOutlined />, path: '/' },
    { key: '/documents', label: '语料', icon: <FolderOpenOutlined />, path: '/documents' },
    { key: '/lab', label: '实验', icon: <ExperimentOutlined />, path: '/lab' },
    { key: '/audit', label: '审计', icon: <SafetyOutlined />, path: '/audit', adminOnly: true },
  ]

  const userMenuItems = [
    { key: 'settings', icon: <SettingOutlined />, label: '个人设置' },
    { type: 'divider' as const },
    { key: 'logout', icon: <LogoutOutlined />, label: '退出登录', danger: true },
  ]

  return (
    <div style={{ minHeight: '100vh', background: 'var(--color-bg)' }}>
      <header
        style={{
          padding: compact ? '10px 12px' : '12px 24px',
          background: 'var(--color-bg-white)',
          borderBottom: '1px solid var(--color-border)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 8,
          position: 'sticky',
          top: 0,
          zIndex: 100,
          minWidth: 0,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: compact ? 8 : 20, minWidth: 0, flex: 1 }}>
          <Link to="/" style={{ display: 'flex', alignItems: 'center', gap: 10, textDecoration: 'none', flexShrink: 0 }}>
            <div
              style={{
                width: compact ? 30 : 36,
                height: compact ? 30 : 36,
                borderRadius: 'var(--radius-md)',
                background: 'linear-gradient(135deg, #0f3b66 0%, #145a8a 100%)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#fff',
                fontSize: compact ? 15 : 18,
                fontWeight: 700,
                flexShrink: 0,
              }}
            >
              F
            </div>
            {!compact && (
              <div style={{ fontSize: 16, fontWeight: 700, color: 'var(--color-text)', whiteSpace: 'nowrap' }}>
                金融查证
              </div>
            )}
          </Link>

          <nav style={{ display: 'flex', alignItems: 'center', gap: 2, overflowX: 'auto', minWidth: 0 }}>
            {navItems
              .filter((item) => !item.adminOnly || isAdmin)
              .map((item) => {
                const active = location.pathname === item.path
                return (
                  <Link
                    key={item.key}
                    to={item.path}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 5,
                      padding: compact ? '5px 8px' : '6px 11px',
                      borderRadius: 'var(--radius-md)',
                      textDecoration: 'none',
                      fontSize: compact ? 12 : 14,
                      whiteSpace: 'nowrap',
                      fontWeight: active ? 500 : 400,
                      color: active ? 'var(--color-primary)' : 'var(--color-text)',
                      background: active ? 'var(--color-primary-light)' : 'transparent',
                      transition: 'all var(--transition-fast)',
                    }}
                  >
                    {item.icon}
                    {item.label}
                  </Link>
                )
              })}
          </nav>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {isAdmin && !compact && (
            <Tag color="red" style={{ marginRight: 8 }}>
              管理员
            </Tag>
          )}

          <Tooltip title={isDark ? '切换亮色模式' : '切换暗色模式'}>
            <Button
              type="text"
              size="small"
              icon={isDark ? <SunOutlined style={{ fontSize: 16 }} /> : <MoonOutlined style={{ fontSize: 16 }} />}
              onClick={onToggleTheme}
              className="btn-hover"
            />
          </Tooltip>

          <Dropdown
            menu={{
              items: userMenuItems,
              onClick: ({ key }) => {
                if (key === 'settings') setSettingsOpen(true)
                else if (key === 'logout') logout()
              },
            }}
            trigger={['click']}
            placement="bottomRight"
          >
            <Button
              type="text"
              size="small"
              icon={<UserOutlined />}
              className="btn-hover"
              style={{ display: 'flex', alignItems: 'center', gap: 4 }}
            >
              <span style={{ fontSize: 13, maxWidth: 80, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {userId || 'anonymous'}
              </span>
            </Button>
          </Dropdown>
        </div>
      </header>

      <main>{children}</main>

      <UserSettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  )
}

export default PageShell
