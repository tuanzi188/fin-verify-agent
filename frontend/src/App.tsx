import React, { useState } from 'react'
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

  return (
    <UserProvider>
      <ConfigProvider
        locale={zhCN}
        theme={{
          algorithm: isDark ? antTheme.darkAlgorithm : antTheme.defaultAlgorithm,
          token: {
            colorPrimary: isDark ? '#597ef7' : '#1677ff',
            borderRadius: 8,
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
