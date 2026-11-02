import React, { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { ConfigProvider, theme as antTheme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { UserProvider } from './contexts/UserContext'
import ChatBox from './components/ChatBox'
import PageShell from './components/PageShell'
import LabDashboard from './pages/LabDashboard'
import FinanceDocuments from './pages/FinanceDocuments'
import AuditLogs from './pages/AuditLogs'

const App: React.FC = () => {
  const [isDark, setIsDark] = useState(() => {
    return localStorage.getItem('theme') === 'dark'
  })

  const toggleTheme = () => {
    const next = !isDark
    setIsDark(next)
    localStorage.setItem('theme', next ? 'dark' : 'light')
  }

  // 主题属性挂到最外层：直接访问 /lab、/documents 时 ChatBox 不渲染，
  // 若只在组件内部设置，自定义 CSS 变量不会切到暗色。
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light')
  }, [isDark])

  return (
    <UserProvider>
      <ConfigProvider
        locale={zhCN}
        theme={{
          algorithm: isDark ? antTheme.darkAlgorithm : antTheme.defaultAlgorithm,
          token: {
            // 深墨蓝：查证台要的是"档案与印章"，不是 SaaS 亮蓝。
            colorPrimary: isDark ? '#8fa8c8' : '#1f3a5f',
            colorInfo: isDark ? '#8fa8c8' : '#1f3a5f',
            borderRadius: 6,
            fontFamily:
              "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
          },
        }}
      >
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<ChatBox isDark={isDark} onToggleTheme={toggleTheme} />} />
            <Route
              path="/lab"
              element={
                <PageShell isDark={isDark} onToggleTheme={toggleTheme}>
                  <LabDashboard />
                </PageShell>
              }
            />
            <Route
              path="/documents"
              element={
                <PageShell isDark={isDark} onToggleTheme={toggleTheme}>
                  <FinanceDocuments />
                </PageShell>
              }
            />
            <Route path="/analytics" element={<Navigate to="/lab" replace />} />
            <Route
              path="/audit"
              element={
                <PageShell isDark={isDark} onToggleTheme={toggleTheme}>
                  <AuditLogs />
                </PageShell>
              }
            />
          </Routes>
        </BrowserRouter>
      </ConfigProvider>
    </UserProvider>
  )
}

export default App
