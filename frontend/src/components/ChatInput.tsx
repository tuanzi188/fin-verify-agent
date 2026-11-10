import React, { useState, useRef } from 'react'
import { Input, Button, Tooltip, Popover, Typography } from 'antd'
import {
  SendOutlined,
  ClearOutlined,
  PlusCircleOutlined,
} from '@ant-design/icons'

const { Text } = Typography

interface ChatInputProps {
  loading: boolean
  onSend: (text: string) => void
  onClearContext?: () => void
  onNewConversation?: () => void
  /** 输入框右侧的附加控件（如"联网"开关），由调用方决定内容。 */
  toolbar?: React.ReactNode
}

const ChatInput: React.FC<ChatInputProps> = ({
  loading,
  onSend,
  onNewConversation,
  toolbar,
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
      <div style={{ fontSize: 13, color: 'var(--color-text-secondary)', lineHeight: 2.2 }}>
        <div><Text keyboard>Enter</Text> 发送消息</div>
        <div><Text keyboard>Shift</Text> + <Text keyboard>Enter</Text> 换行</div>
        <div><Text keyboard>Ctrl</Text> + <Text keyboard>K</Text> 新建查证</div>
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
          boxShadow: focused ? '0 0 0 3px var(--color-focus-ring)' : 'none',
        }}
      >
        {/* 左侧快捷功能栏 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 2, marginBottom: 2 }}>
          <Tooltip title="新建查证">
            <Button
              type="text"
              size="small"
              icon={<PlusCircleOutlined />}
              onClick={onNewConversation}
              style={{ color: 'var(--color-text-tertiary)' }}
            />
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
          placeholder={loading ? "正在查证…" : "输入问题，查证金融文档"}
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

        {/* 发送按钮 */}
        {toolbar}
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
        <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>
          {loading ? '正在检索证据并生成结论' : '金融查证 · 结构检索 / 多步查证'}
        </span>
        <Popover
          content={shortcutContent}
          title="快捷键说明"
          trigger="hover"
          placement="topRight"
        >
          <Text
            className="chat-shortcut-hint"
            style={{
              fontSize: 12,
              color: 'var(--color-text-tertiary)',
              cursor: 'help',
              userSelect: 'none',
              transition: 'color var(--transition-fast)',
            }}
          >
            Enter 发送 · Shift+Enter 换行
          </Text>
        </Popover>
      </div>
    </div>
  )
}

export default ChatInput
