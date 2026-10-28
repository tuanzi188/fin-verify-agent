import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import React from 'react'
import ChatInput from '../components/ChatInput'

describe('ChatInput', () => {
  it('renders textarea and send button', () => {
    render(<ChatInput loading={false} onSend={vi.fn()} />)
    expect(screen.getByPlaceholderText(/输入问题/)).toBeTruthy()
    expect(screen.getByLabelText('发送')).toBeTruthy()
  })

  it('calls onSend when send button is clicked with text', () => {
    const onSend = vi.fn()
    render(<ChatInput loading={false} onSend={onSend} />)
    const textarea = screen.getByPlaceholderText(/输入问题/)
    fireEvent.change(textarea, { target: { value: 'Hello' } })
    fireEvent.click(screen.getByLabelText('发送'))
    expect(onSend).toHaveBeenCalledWith('Hello')
  })

  it('does not call onSend when input is empty', () => {
    const onSend = vi.fn()
    render(<ChatInput loading={false} onSend={onSend} />)
    fireEvent.click(screen.getByLabelText('发送'))
    expect(onSend).not.toHaveBeenCalled()
  })

  it('does not call onSend when loading', () => {
    const onSend = vi.fn()
    render(<ChatInput loading={true} onSend={onSend} />)
    const textarea = screen.getByPlaceholderText(/输入问题/)
    fireEvent.change(textarea, { target: { value: 'Hello' } })
    fireEvent.click(screen.getByLabelText('发送'))
    expect(onSend).not.toHaveBeenCalled()
  })

  it('shows help text', () => {
    render(<ChatInput loading={false} onSend={vi.fn()} />)
    expect(screen.getByText(/Enter 发送/)).toBeTruthy()
  })

  it('toggles agent mode through the switch', () => {
    const onAgentModeChange = vi.fn()
    render(
      <ChatInput
        loading={false}
        onSend={vi.fn()}
        agentMode={false}
        onAgentModeChange={onAgentModeChange}
      />
    )
    fireEvent.click(screen.getByLabelText('深度检索'))
    expect(onAgentModeChange).toHaveBeenCalledWith(true)
  })
})
