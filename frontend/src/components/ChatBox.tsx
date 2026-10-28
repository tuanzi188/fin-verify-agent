import React, { useState, useRef, useEffect, useCallback } from 'react'
import { message, Spin, Typography } from 'antd'
import ReactMarkdown from 'react-markdown'
import { DisplayCitation, uploadDocument, getStats } from '../api/client'
import { useChat } from '../hooks/useChat'
import { useConversations } from '../hooks/useConversations'
import MessageItem from './MessageItem'
import ChatSidebar from './ChatSidebar'
import ChatHeader from './ChatHeader'
import ChatInput from './ChatInput'
import WelcomeScreen from './WelcomeScreen'
import CitationPanel from './CitationPanel'
import DocumentManager from './DocumentManager'
import DocumentUpload from './DocumentUpload'
import { ConnectionState } from '../types/connection'

const { Text } = Typography

interface ChatBoxProps {
  isDark?: boolean
  onToggleTheme?: () => void
}

const ChatBox: React.FC<ChatBoxProps> = ({ isDark, onToggleTheme }) => {
  const [selectedCitation, setSelectedCitation] = useState<DisplayCitation | null>(null)
  const [stats, setStats] = useState<{ total_chunks: number } | null>(null)
  const [documentManagerOpen, setDocumentManagerOpen] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [sidebarVisible, setSidebarVisible] = useState(true)
  const [connectionState, setConnectionState] = useState<ConnectionState>('connecting')
  const [agentMode, setAgentMode] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const {
    conversations,
    conversationsLoading,
    activeConversationId,
    loadConversations,
    loadConversation,
    deleteConv,
    startNewConversation,
  } = useConversations()

  const {
    messages,
    loading,
    streamingContent,
    conversationId: chatConversationId,
    sendMessage,
    stopGeneration,
    loadHistoryMessages,
    startNew,
    setConversationId,
  } = useChat(() => {
    loadConversations()
  })

  // 单一状态源：发送成功后 useChat 会拿到后端返回的会话 id，侧栏高亮以此为准，
  // 避免 activeConversationId 与 chatConversationId 各说各话。
  const currentConversationId = chatConversationId || activeConversationId

  // 检测连接状态
  useEffect(() => {
    const checkConnection = async () => {
      try {
        await getStats()
        setConnectionState('connected')
      } catch {
        setConnectionState('disconnected')
      }
    }
    checkConnection()
    const interval = setInterval(checkConnection, 30000)
    return () => clearInterval(interval)
  }, [])

  // 暗色模式 data 属性
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light')
  }, [isDark])

  useEffect(() => {
    getStats().then(setStats).catch((err) => console.error('获取统计信息失败:', err))
  }, [])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingContent])

  const handleSelectConversation = async (convId: string) => {
    try {
      const turns = await loadConversation(convId)
      setConversationId(convId)
      loadHistoryMessages(turns)
    } catch (err) {
      console.error('加载对话失败:', err)
      message.error('加载对话失败')
    }
  }

  const handleNewConversation = () => {
    startNewConversation()
    startNew()
  }

  const handleDeleteConversation = async (convId: string) => {
    try {
      await deleteConv(convId)
      message.success('对话已删除')
      if (currentConversationId === convId) {
        startNew()
      }
    } catch (err) {
      console.error('删除对话失败:', err)
      message.error('删除失败')
    }
  }

  const handleUpload = async (file: File) => {
    const hide = message.loading(`正在上传 ${file.name}...`)
    try {
      const result = await uploadDocument(file)
      hide()
      message.success(`${file.name} 上传成功，已解析为 ${result.chunks_count} 个知识片段`)
      getStats().then(setStats).catch((err) => console.error('获取统计信息失败:', err))
    } catch (err) {
      console.error('文件上传失败:', err)
      hide()
      message.error(`${file.name} 上传失败`)
    }
  }

  const handleQuickQuestion = useCallback((question: string) => {
    sendMessage(question)
  }, [sendMessage])

  const handleReconnect = useCallback(async () => {
    setConnectionState('connecting')
    try {
      await getStats()
      setConnectionState('connected')
      message.success('已重新连接')
    } catch {
      setConnectionState('disconnected')
      message.error('重连失败，请稍后再试')
    }
  }, [])

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text)
    message.success('已复制到剪贴板')
  }

  const formatDate = (ts: number) => {
    const date = new Date(ts * 1000)
    const now = new Date()
    const diff = now.getTime() - date.getTime()
    const oneDay = 24 * 60 * 60 * 1000

    if (diff < oneDay && now.getDate() === date.getDate()) {
      return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    } else if (diff < 2 * oneDay) {
      return '昨天 ' + date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    } else if (diff < 7 * oneDay) {
      const days = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
      return days[date.getDay()]
    }
    return date.toLocaleDateString('zh-CN')
  }

  const hasDocuments = (stats?.total_chunks ?? 0) > 0

  return (
    <div
      style={{
        display: 'flex',
        height: '100vh',
        background: 'var(--color-bg)',
        transition: 'background var(--transition-normal)',
      }}
    >
      <ChatSidebar
        visible={sidebarVisible}
        conversations={conversations}
        loading={conversationsLoading}
        activeConversationId={currentConversationId}
        onNewConversation={handleNewConversation}
        onSelectConversation={handleSelectConversation}
        onDeleteConversation={handleDeleteConversation}
        onToggleSidebar={() => setSidebarVisible(!sidebarVisible)}
        formatDate={formatDate}
      />

      {/* 主内容区域 - 1440px 最大宽度居中 */}
      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          maxWidth: 'var(--max-width)',
          margin: '0 auto',
          width: '100%',
          minWidth: 0,
        }}
      >
        <ChatHeader
          isDark={!!isDark}
          stats={stats}
          sidebarVisible={sidebarVisible}
          onToggleSidebar={() => setSidebarVisible(!sidebarVisible)}
          onToggleTheme={onToggleTheme || (() => {})}
          onOpenUpload={() => setUploadOpen(true)}
          onOpenDocumentManager={() => setDocumentManagerOpen(true)}
          connectionState={connectionState}
          onReconnect={handleReconnect}
        />

        <div
          className="chat-content"
          style={{
            flex: 1,
            overflow: 'auto',
            padding: '16px',
            transition: 'background var(--transition-normal)',
          }}
        >
          {messages.length === 0 && !streamingContent && (
            <WelcomeScreen
              hasDocuments={hasDocuments}
              documentCount={stats?.total_chunks ?? 0}
              onUpload={handleUpload}
              onQuickQuestion={handleQuickQuestion}
            />
          )}

          {messages.map((msg) => (
            <MessageItem
              key={msg.id}
              role={msg.role}
              content={msg.content}
              citations={msg.citations}
              confidence={msg.confidence}
              model={msg.model}
              mode={msg.mode}
              reasoning_content={msg.reasoning_content}
              isFallback={msg.isFallback}
              status={msg.status}
              trace={msg.trace}
              onCitationClick={setSelectedCitation}
              onCopy={copyToClipboard}
            />
          ))}

          {loading && streamingContent && (
            <div className="animate-fade-in" style={{ display: 'flex', gap: 10, marginBottom: 16, alignItems: 'flex-start' }}>
              <div style={{
                width: 34, height: 34, borderRadius: 'var(--radius-md)',
                background: 'linear-gradient(135deg, #52c41a 0%, #389e0d 100%)',
                display: 'flex', alignItems: 'center',
                justifyContent: 'center', color: '#fff', fontSize: 14, fontWeight: 700,
                flexShrink: 0,
                boxShadow: 'var(--shadow-sm)',
              }}>K</div>
              <div style={{
                background: 'var(--color-bg-white)',
                borderRadius: 'var(--radius-lg)',
                padding: '12px 16px',
                boxShadow: 'var(--shadow-sm)',
                border: '1px solid var(--color-border)',
                maxWidth: '85%',
                transition: 'background var(--transition-normal), border var(--transition-normal)',
              }}>
                <div className="markdown-body" style={{ color: 'var(--color-text)', fontSize: 14, lineHeight: 1.6 }}>
                  <ReactMarkdown>{streamingContent}</ReactMarkdown>
                  <span className="streaming-cursor" />
                </div>
              </div>
            </div>
          )}

          {loading && !streamingContent && (
            <div className="animate-fade-in" style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
              <div style={{
                width: 32, height: 32, borderRadius: 'var(--radius-md)',
                background: 'linear-gradient(135deg, #1677ff 0%, #0958d9 100%)',
                display: 'flex', alignItems: 'center',
                justifyContent: 'center', color: '#fff', fontSize: 14, fontWeight: 700,
                flexShrink: 0,
              }}>K</div>
              <div style={{
                background: 'var(--color-bg-white)',
                borderRadius: 'var(--radius-lg)',
                padding: '12px 16px',
                boxShadow: 'var(--shadow-sm)',
                transition: 'background var(--transition-normal)',
              }}>
                <Spin size="small" />
                <Text style={{ marginLeft: 8, color: 'var(--color-text-secondary)' }}>
                  {agentMode ? '正在多步检索...' : '正在思考...'}
                </Text>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        <ChatInput
          loading={loading}
          onSend={(text) => sendMessage(text, { mode: agentMode ? 'agent' : 'rag' })}
          onStop={stopGeneration}
          onNewConversation={handleNewConversation}
          onReferenceDocument={() => setDocumentManagerOpen(true)}
          agentMode={agentMode}
          onAgentModeChange={setAgentMode}
        />
      </div>

      <CitationPanel
        citation={selectedCitation}
        onClose={() => setSelectedCitation(null)}
      />

      <DocumentManager
        open={documentManagerOpen}
        onClose={() => {
          setDocumentManagerOpen(false)
          getStats().then(setStats).catch(() => {})
        }}
      />

      <DocumentUpload
        open={uploadOpen}
        onClose={() => {
          setUploadOpen(false)
          getStats().then(setStats).catch(() => {})
        }}
      />
    </div>
  )
}

export default ChatBox