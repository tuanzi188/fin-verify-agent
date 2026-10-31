import React, { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { message, Spin, Typography } from 'antd'
import {
  CalcRecord,
  EvidenceRef,
  FinanceDocumentInfo,
  FinanceStats,
  getFinanceDocuments,
  getFinanceStats,
  getHealth,
} from '../api/client'
import { useChat } from '../hooks/useChat'
import { useConversations } from '../hooks/useConversations'
import { useMediaQuery } from '../hooks/useMediaQuery'
import { groupTurns } from '../utils/turns'
import { deriveStatus, type TurnStatus } from '../utils/status'
import MessageItem from './MessageItem'
import ChatSidebar from './ChatSidebar'
import ChatHeader from './ChatHeader'
import ChatInput from './ChatInput'
import WelcomeScreen from './WelcomeScreen'
import EvidenceInspector from './EvidenceInspector'
import { TurnQuestion, TurnStrip } from './WorkbenchTurns'
import { ConnectionState } from '../types/connection'

const { Text } = Typography

interface ChatBoxProps {
  isDark?: boolean
  onToggleTheme?: () => void
}

const ChatBox: React.FC<ChatBoxProps> = ({ isDark, onToggleTheme }) => {
  const isMobile = useMediaQuery('(max-width: 767px)')
  const isCompact = useMediaQuery('(max-width: 1439px)')
  // ≥1280 三栏：260 侧栏 + 640 主列 + 380 证据列，正好满足主列可读宽度。
  const isWide = useMediaQuery('(min-width: 1280px)')

  const [selectedEvidence, setSelectedEvidence] = useState<EvidenceRef | null>(null)
  const [selectedCalc, setSelectedCalc] = useState<CalcRecord | null>(null)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const [stats, setStats] = useState<FinanceStats | null>(null)
  const [materials, setMaterials] = useState<FinanceDocumentInfo[]>([])
  const [materialsLoading, setMaterialsLoading] = useState(false)
  // null = 跟随最新一轮；点轮次条时才固定在某一轮。
  const [activeTurnIndex, setActiveTurnIndex] = useState<number | null>(null)
  // 证据回查 404 的轮次：标成“需人工复核”，不修改后端原始 status。
  const [reviewTurns, setReviewTurns] = useState<Set<number>>(() => new Set())
  // 首帧就按断点决定，避免移动端先渲染 Drawer 再被 effect 关掉（会闪一下遮罩）。
  const [sidebarVisible, setSidebarVisible] = useState(() => !isMobile)
  const [connectionState, setConnectionState] = useState<ConnectionState>('connecting')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const currentTurnIndexRef = useRef(-1)

  useEffect(() => {
    setSidebarVisible(!isMobile)
  }, [isMobile])

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
    conversationId: chatConversationId,
    sendMessage,
    loadHistoryMessages,
    startNew,
    setConversationId,
  } = useChat(() => {
    loadConversations()
  })

  const currentConversationId = chatConversationId || activeConversationId

  const loadFinanceStats = useCallback(() => {
    getFinanceStats()
      .then(setStats)
      .catch((err) => console.error('获取金融语料统计失败:', err))
  }, [])

  const loadMaterials = useCallback(() => {
    setMaterialsLoading(true)
    getFinanceDocuments()
      .then((result) => setMaterials(result.documents || []))
      .catch((err) => console.error('获取查证材料失败:', err))
      .finally(() => setMaterialsLoading(false))
  }, [])

  // 连接状态与语料统计分开：后端可用但金融语料未导入时，
  // 页面应该显示“语料未就绪”，而不是把整个服务判成离线。
  useEffect(() => {
    const checkConnection = async () => {
      try {
        await getHealth()
        setConnectionState('connected')
      } catch {
        setConnectionState('disconnected')
      }
    }
    checkConnection()
    const interval = setInterval(checkConnection, 30000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light')
  }, [isDark])

  useEffect(() => {
    loadFinanceStats()
    loadMaterials()
  }, [loadFinanceStats, loadMaterials])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSelectConversation = async (convId: string) => {
    try {
      const historyTurns = await loadConversation(convId)
      setConversationId(convId)
      loadHistoryMessages(historyTurns)
      setActiveTurnIndex(null)
      setReviewTurns(new Set())
      setInspectorOpen(false)
    } catch (err) {
      console.error('加载对话失败:', err)
      message.error('加载对话失败')
    }
  }

  const handleNewConversation = () => {
    startNewConversation()
    startNew()
    setActiveTurnIndex(null)
    setReviewTurns(new Set())
    setInspectorOpen(false)
    setSelectedEvidence(null)
    setSelectedCalc(null)
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

  const handleQuickQuestion = useCallback((question: string) => {
    setActiveTurnIndex(null)
    sendMessage(question)
  }, [sendMessage])

  const handleSend = useCallback((text: string) => {
    // 新问题一定切到最新一轮，否则会停留在旧的查证记录上。
    setActiveTurnIndex(null)
    sendMessage(text)
  }, [sendMessage])

  const handleReconnect = useCallback(async () => {
    setConnectionState('connecting')
    try {
      await getHealth()
      setConnectionState('connected')
      loadFinanceStats()
      message.success('已重新连接')
    } catch {
      setConnectionState('disconnected')
      message.error('重连失败，请稍后再试')
    }
  }, [loadFinanceStats])

  const handleEvidenceClick = useCallback(
    (evidence: EvidenceRef | null, calc?: CalcRecord | null) => {
      setSelectedEvidence(evidence)
      setSelectedCalc(calc || null)
      setInspectorOpen(true)
    },
    [],
  )

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

  /** 消息时间戳是毫秒；formatDate 处理的是会话列表的秒级时间，别混用。 */
  const formatClock = (ms?: number) =>
    typeof ms === 'number'
      ? new Date(ms).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      : undefined

  const hasDocuments = (stats?.documents ?? 0) > 0

  const turns = useMemo(() => groupTurns(messages), [messages])
  const latestTurnIndex = turns.length - 1
  const currentTurnIndex =
    activeTurnIndex === null ? latestTurnIndex : Math.min(activeTurnIndex, latestTurnIndex)
  const currentTurn = currentTurnIndex >= 0 ? turns[currentTurnIndex] : undefined
  const currentAnswer = currentTurn?.answer
  const activeTrace = currentAnswer?.trace || []
  const currentCitations = currentAnswer?.finance?.citations || []
  const currentFingerprint = stats?.index_fingerprint || ''
  const activeDocIds = Array.from(
    new Set(currentCitations.map((citation) => citation.evidence.doc_id).filter(Boolean)),
  )

  // 证据可核验性 → 第五种状态：语料版本变化、无引用结论、回查 404。
  const turnStatuses: (TurnStatus | undefined)[] = useMemo(
    () =>
      turns.map((turn, index) => {
        if (!turn.answer) return undefined
        const finance = turn.answer.finance
        const savedFingerprint = finance?.corpus_fingerprint
        const corpusChanged = Boolean(
          savedFingerprint && currentFingerprint && savedFingerprint !== currentFingerprint,
        )
        const base = deriveStatus(turn.answer.status, {
          corpusChanged,
          hasClaims: (finance?.claims.length || 0) > 0,
          hasCitations: (finance?.citations.length || 0) > 0,
        })
        return reviewTurns.has(index) && base === 'answered' ? 'needs_review' : base
      }),
    [turns, currentFingerprint, reviewTurns],
  )
  const currentStatus = currentTurnIndex >= 0 ? turnStatuses[currentTurnIndex] : undefined
  const currentCorpusStatus = (() => {
    const savedFingerprint = currentAnswer?.finance?.corpus_fingerprint
    if (!savedFingerprint || !currentFingerprint) return 'unknown' as const
    return savedFingerprint === currentFingerprint ? ('current' as const) : ('changed' as const)
  })()

  const handleVerificationIssue = useCallback((_evidenceId: string) => {
    // 回查 404：当前这一轮从“已回答”降级为“需人工复核”。
    // 用 ref 取最新轮次，保持回调稳定，避免切换轮次时重复回查证据。
    const index = currentTurnIndexRef.current
    setReviewTurns((prev) => {
      if (prev.has(index)) return prev
      const next = new Set(prev)
      next.add(index)
      return next
    })
  }, [])

  // 切换查证轮次时回到顶部：结论卡从问题头开始读，而不是停在上一轮的滚动位置。
  useEffect(() => {
    currentTurnIndexRef.current = currentTurnIndex
    contentRef.current?.scrollTo({ top: 0 })
    // 上一轮选中的证据不属于当前轮，必须清掉，否则右栏会挂着无关引用。
    setSelectedEvidence(null)
    setSelectedCalc(null)
  }, [currentTurnIndex])

  return (
    <div
      style={{
        display: 'flex',
        height: '100vh',
        background: 'var(--color-bg)',
        transition: 'background var(--transition-normal)',
        overflow: 'hidden',
      }}
    >
      <ChatSidebar
        visible={sidebarVisible}
        mobile={isMobile}
        conversations={conversations}
        loading={conversationsLoading}
        activeConversationId={currentConversationId}
        onNewConversation={handleNewConversation}
        onSelectConversation={handleSelectConversation}
        onDeleteConversation={handleDeleteConversation}
        onClose={() => setSidebarVisible(false)}
        formatDate={formatDate}
        materials={materials}
        materialsLoading={materialsLoading}
        coverage={stats}
        activeDocIds={activeDocIds}
      />

      <div style={{ flex: 1, display: 'flex', minWidth: 0, minHeight: 0 }}>
        {/* 主查证列 */}
        <div
          style={{
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            maxWidth: isWide ? undefined : 'var(--max-width)',
            margin: isWide ? 0 : '0 auto',
            width: '100%',
            minWidth: 0,
            minHeight: 0,
          }}
        >
          <ChatHeader
            isDark={!!isDark}
            stats={stats}
            sidebarVisible={sidebarVisible}
            onToggleSidebar={() => setSidebarVisible((prev) => !prev)}
            onToggleTheme={onToggleTheme || (() => {})}
            connectionState={connectionState}
            onReconnect={handleReconnect}
            mobile={isMobile}
            compact={isCompact}
          />

          {/* 历史不铺在正文里：一轮一次查证，用轮次条切换。 */}
          <TurnStrip
            turns={turns}
            statuses={turnStatuses}
            activeIndex={currentTurnIndex}
            onSelect={setActiveTurnIndex}
          />

          <div
            className="chat-content"
            ref={contentRef}
            style={{
              flex: 1,
              overflow: 'auto',
              padding: isMobile ? '8px' : '16px',
              transition: 'background var(--transition-normal)',
              minHeight: 0,
            }}
          >
            {messages.length === 0 && (
              <WelcomeScreen
                hasDocuments={hasDocuments}
                stats={stats}
                onQuickQuestion={handleQuickQuestion}
              />
            )}

            {currentTurn && (
              <div className="animate-fade-in" style={{ maxWidth: 920, margin: '0 auto' }}>
                <TurnQuestion
                  turn={currentTurn}
                  index={currentTurnIndex}
                  status={currentStatus}
                  timestampLabel={
                    currentTurn.question ? formatClock(currentTurn.question.timestamp) : undefined
                  }
                />

                {currentAnswer ? (
                  <MessageItem
                    plain
                    content={currentAnswer.content}
                    trace={currentAnswer.trace}
                    finance={currentAnswer.finance}
                    onEvidenceClick={handleEvidenceClick}
                    onCopy={copyToClipboard}
                  />
                ) : loading ? (
                  <div
                    className="animate-fade-in"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '12px 14px',
                      borderRadius: 'var(--radius-lg)',
                      background: 'var(--color-bg-white)',
                      border: '1px solid var(--color-border)',
                      boxShadow: 'var(--shadow-sm)',
                    }}
                  >
                    <Spin size="small" />
                    <Text style={{ color: 'var(--color-text-secondary)' }}>
                      正在查证：自动选择结构检索或多步查证
                    </Text>
                  </div>
                ) : (
                  <Text style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>
                    这一轮没有回答记录。
                  </Text>
                )}
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>

          <ChatInput
            loading={loading}
            onSend={handleSend}
            onNewConversation={handleNewConversation}
          />
        </div>

        {/* ≥1280：证据检查器常驻，形成三栏 */}
        {isWide && (
          <EvidenceInspector
            inline
            open
            evidence={selectedEvidence}
            calc={selectedCalc}
            calcRecords={currentAnswer?.finance?.calc || []}
            trace={activeTrace || []}
            evidenceList={currentCitations}
            onSelectEvidence={(ev) => handleEvidenceClick(ev)}
            corpusStatus={currentCorpusStatus}
            onVerificationIssue={handleVerificationIssue}
            onClose={() => {
              setInspectorOpen(false)
              setSelectedEvidence(null)
              setSelectedCalc(null)
            }}
            onSelectCalc={setSelectedCalc}
          />
        )}
      </div>

      {/* <1280：右侧 Drawer；移动端改为底部 Sheet */}
      {!isWide && (
        <EvidenceInspector
          open={inspectorOpen}
          mobile={isMobile}
          evidence={selectedEvidence}
          calc={selectedCalc}
          calcRecords={currentAnswer?.finance?.calc || []}
          trace={activeTrace || []}
          evidenceList={currentCitations}
          onSelectEvidence={(ev) => handleEvidenceClick(ev)}
          corpusStatus={currentCorpusStatus}
          onVerificationIssue={handleVerificationIssue}
          onClose={() => {
            setInspectorOpen(false)
            setSelectedEvidence(null)
            setSelectedCalc(null)
          }}
          onSelectCalc={setSelectedCalc}
        />
      )}
    </div>
  )
}

export default ChatBox
