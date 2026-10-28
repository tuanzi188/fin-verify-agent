import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import React from 'react'
import MessageItem from '../components/MessageItem'

describe('MessageItem', () => {
  it('renders user message correctly', () => {
    render(
      <MessageItem
        role="user"
        content="Hello World"
      />
    )
    expect(screen.getByText('Hello World')).toBeTruthy()
    expect(screen.getByText('U')).toBeTruthy()
  })

  it('renders assistant message with markdown', () => {
    render(
      <MessageItem
        role="assistant"
        content="**Bold** text"
      />
    )
    expect(screen.getByText('Bold')).toBeTruthy()
    expect(screen.getByText('K')).toBeTruthy()
  })

  it('renders confidence tag when provided', () => {
    render(
      <MessageItem
        role="assistant"
        content="Answer"
        confidence={0.85}
      />
    )
    expect(screen.getByText('置信度: 85%')).toBeTruthy()
  })

  it('renders fallback tag when isFallback is true', () => {
    render(
      <MessageItem
        role="assistant"
        content="Answer"
        isFallback={true}
      />
    )
    expect(screen.getByText('兜底回答')).toBeTruthy()
  })

  it('renders model and mode info', () => {
    render(
      <MessageItem
        role="assistant"
        content="Answer"
        model="deepseek-chat"
        mode="flash"
      />
    )
    expect(screen.getByText('deepseek-chat')).toBeTruthy()
    expect(screen.getByText('flash')).toBeTruthy()
  })

  it('calls onCopy when copy button clicked', () => {
    const onCopy = vi.fn()
    render(
      <MessageItem
        role="assistant"
        content="Test content"
        onCopy={onCopy}
      />
    )
    const copyButtons = screen.getAllByRole('button')
    const copyBtn = copyButtons.find(btn => btn.querySelector('.anticon-copy'))
    if (copyBtn) {
      fireEvent.click(copyBtn)
      expect(onCopy).toHaveBeenCalledWith('Test content')
    }
  })

  it('renders citation tags when provided', () => {
    render(
      <MessageItem
        role="assistant"
        content="Answer"
        citations={[
          { document_id: '1', document_title: 'Doc One', content: '...', score: 0.9 },
          { document_id: '2', document_title: 'Doc Two', content: '...', score: 0.8 },
        ]}
      />
    )
    expect(screen.getByText('Doc One')).toBeTruthy()
    expect(screen.getByText('Doc Two')).toBeTruthy()
  })

  it('renders agent status tag', () => {
    render(<MessageItem role="assistant" content="无法确定。" status="abstained" />)
    expect(screen.getByText('资料不足')).toBeTruthy()
  })

  it('collapses agent trace by default and expands on click', () => {
    render(
      <MessageItem
        role="assistant"
        content="Answer"
        status="answered"
        trace={[
          { step: 1, tool: 'search_kb', ok: true, result_count: 3, latency_ms: 42 },
          { step: 2, tool: '__final__', ok: true },
        ]}
      />
    )
    expect(screen.getByText('执行轨迹')).toBeTruthy()
    expect(screen.queryByText('search_kb')).toBeNull()

    fireEvent.click(screen.getByText('执行轨迹'))

    expect(screen.getByText('search_kb')).toBeTruthy()
    expect(screen.getByText('3 条')).toBeTruthy()
    expect(screen.getByText('42 ms')).toBeTruthy()
  })

  it('handles agent citations without a score', () => {
    render(
      <MessageItem
        role="assistant"
        content="住宿标准为每晚五百元 [1]。"
        citations={[
          { document_id: 'd2', document_title: '差旅与报销制度', content: '住宿标准...' },
        ]}
      />
    )
    expect(screen.getByText('差旅与报销制度')).toBeTruthy()
  })
})