import React, { useState, useRef } from 'react'
import { Input, Button, Tooltip, Popover, Typography, Switch, Space } from 'antd'
import {
  SendOutlined,
  StopOutlined,
  ClearOutlined,
  FileSearchOutlined,
  PlusCircleOutlined,
  ExperimentOutlined,
} from '@ant-design/icons'

const { Text } = Typography

interface ChatInputProps {
  loading: boolean
  onSend: (text: string) => void
  onStop?: () => void
  onClearContext?: () => void
  onNewConversation?: () => void
  onReferenceDocument?: () => void
  agentMode?: boolean
  onAgentModeChange?: (enabled: boolean) => void
}

const ChatInput: React.FC<ChatInputProps> = ({
  loading,
  onSend,
  onStop,
  onClearContext,
  onNewConversation,
  onReferenceDocument,
  agentMode = false,
  onAgentModeChange,
}) => {
  const [value, setValue] = useState('')
  const [focused, setFocused] = useState(false)
  const textareaRef = useRef<any>(null)

  const isEmpty = value.trim().length === 0

  const handleSend = () => {
    const text = value.trim()
    if (!text || loading) return
    onSend(text)
    setValue('')
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape' && loading && onStop) {
      e.preventDefault()
      onStop()
      return
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleClear = () => {
    setValue('')
    textareaRef.current?.focus()
  }

  const shortcutContent = (
    <div style={{ minWidth: 200 }}>
      <div style={{ fontSize: 12, color: 'var(--color-text-secondary)', lineHeight: 2.2 }}>
        <div><Text keyboard>Enter</Text> 发送消息</div>
        <div><Text keyboard>Shift</Text> + <Text keyboard>Enter</Text> 换行</div>
        <div><Text keyboard>Esc</Text> 停止生成 / 清空输入</div>
        <div><Text keyboard>Ctrl</Text> + <Text keyboard>K</Text> 新建对话</div>
      </div>
    </div>
  )

  return (
    <div
      style={{
        padding: '12px 16px 16px',
        borderTop: '1px solid var(--color-border)',
        background: 'var(--color-bg-white)',
        transition: 'background var(--transition-normal)',
      }}
    >
      {/* 输入框容器 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-end',
          gap: 8,
          padding: '8px 12px',
          borderRadius: 'var(--radius-lg)',
          border: `1.5px solid ${focused ? 'var(--color-primary)' : 'var(--color-border)'}`,
          background: 'var(--color-bg)',
          transition: 'border-color var(--transition-fast), box-shadow var(--transition-fast)',
          boxShadow: focused ? '0 0 0 3px rgba(22, 119, 255, 0.08)' : 'none',
        }}
      >
        {/* 左侧快捷功能栏 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 2, marginBottom: 2 }}>
          <Tooltip title="新建对话">
            <Button
              type="text"
              size="small"
              icon={<PlusCircleOutlined />}
              onClick={onNewConversation}
              style={{ color: 'var(--color-text-tertiary)' }}
            />
          </Tooltip>
          <Tooltip title="引用文档">
            <Button
              type="text"
              size="small"
              icon={<FileSearchOutlined />}
              onClick={onReferenceDocument}
              style={{ color: 'var(--color-text-tertiary)' }}
            />
          </Tooltip>
          <Tooltip title="深度检索：单轮多步检索，回答后可在消息下方展开执行轨迹">
            <Space size={4} style={{ marginLeft: 4, cursor: 'pointer' }}>
              <Switch
                size="small"
                checked={agentMode}
                onChange={(checked) => onAgentModeChange?.(checked)}
                aria-label="深度检索"
              />
              <Text
                style={{
                  fontSize: 12,
                  color: agentMode ? 'var(--color-primary)' : 'var(--color-text-tertiary)',
                }}
              >
                <ExperimentOutlined style={{ marginRight: 3 }} />
                深度
              </Text>
            </Space>
          </Tooltip>
          {value && (
            <Tooltip title="清空输入">
              <Button
                type="text"
                size="small"
                icon={<ClearOutlined />}
                onClick={handleClear}
                style={{ color: 'var(--color-text-tertiary)' }}
              />
            </Tooltip>
          )}
        </div>

        {/* 文本输入区 */}
        <Input.TextArea
          ref={textareaRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder={loading ? "输入问题，基于知识库智能问答 (正在生成，可按 Esc 停止)" : "输入问题，基于知识库智能问答 (Enter 发送)"}
          autoSize={{ minRows: 1, maxRows: 6 }}
          variant="borderless"
          style={{
            flex: 1,
            padding: '4px 0',
            fontSize: 14,
            lineHeight: 1.6,
            resize: 'none',
          }}
        />

        {/* 发送 / 停止生成 按钮 */}
        {loading && onStop ? (
          <Tooltip title="停止生成 (Esc)">
            <Button
              danger
              type="primary"
              icon={<StopOutlined />}
              aria-label="停止生成"
              onClick={onStop}
              className="btn-hover"
              style={{
                minWidth: 36,
                height: 36,
                borderRadius: 'var(--radius-md)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                transition: 'all var(--transition-fast)',
                flexShrink: 0,
              }}
            />
          </Tooltip>
        ) : (
          <Tooltip title={isEmpty ? '请输入内容' : '发送 (Enter)'}>
            <Button
              type="primary"
              icon={<SendOutlined />}
              aria-label="发送"
              onClick={handleSend}
              disabled={isEmpty || loading}
              className="btn-hover"
              style={{
                minWidth: 36,
                height: 36,
                borderRadius: 'var(--radius-md)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                opacity: isEmpty || loading ? 0.4 : 1,
                transition: 'all var(--transition-fast)',
                flexShrink: 0,
              }}
            />
          </Tooltip>
        )}
      </div>

      {/* 底部快捷键与提示 */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginTop: 6,
          padding: '0 4px',
        }}
      >
        <span style={{ fontSize: 11, color: 'var(--color-text-tertiary)' }}>
          {loading ? '⚡ 正在流式生成回答' : '知识库检索已就绪'}
        </span>
        <Popover
          content={shortcutContent}
          title="快捷键说明"
          trigger="hover"
          placement="topRight"
        >
          <Text
            style={{
              fontSize: 11,
              color: 'var(--color-text-tertiary)',
              cursor: 'help',
              userSelect: 'none',
              transition: 'color var(--transition-fast)',
            }}
          >
            Enter 发送 · Shift+Enter 换行 · Esc 停止
          </Text>
        </Popover>
      </div>
    </div>
  )
}

export default ChatInput
