import React from 'react'

interface ErrorBoundaryProps {
  children: React.ReactNode
}

interface ErrorBoundaryState {
  error: Error | null
}

/**
 * 顶层错误边界。
 *
 * React 在渲染期抛出异常时，如果没有错误边界，整棵组件树会被卸载，用户看到的是
 * 一片白屏，且没有任何提示。任何一个接口返回结构不符、某个字段为 undefined，
 * 都可能触发这种结果。这里兜住它，降级为可读提示 + 重新加载入口。
 */
class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    console.error('界面渲染出错，已被错误边界捕获:', error, info.componentStack)
  }

  private handleReload = (): void => {
    this.setState({ error: null })
    window.location.reload()
  }

  render(): React.ReactNode {
    const { error } = this.state
    if (!error) {
      return this.props.children
    }

    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: 24,
          background: 'var(--color-bg, #f5f6f8)',
        }}
      >
        <div
          style={{
            maxWidth: 520,
            width: '100%',
            textAlign: 'center',
            background: 'var(--color-bg-white, #fff)',
            border: '1px solid var(--color-border, #e5e7eb)',
            borderRadius: 'var(--radius-lg, 12px)',
            padding: '28px 24px',
          }}
        >
          <div style={{ fontSize: 40, lineHeight: 1, marginBottom: 12 }}>😵</div>
          <h2 style={{ margin: '0 0 8px', fontSize: 18, color: 'var(--color-text, #1f2328)' }}>
            页面渲染出错了
          </h2>
          <p
            style={{
              margin: '0 0 16px',
              fontSize: 13,
              lineHeight: 1.7,
              color: 'var(--color-text-secondary, #6b7280)',
            }}
          >
            界面遇到未预期的错误，已阻止白屏。可以先重新加载；若反复出现，请把下面的错误信息反馈给我们。
          </p>
          <pre
            style={{
              textAlign: 'left',
              margin: '0 0 16px',
              padding: 12,
              borderRadius: 8,
              background: 'var(--color-bg, #f5f6f8)',
              border: '1px solid var(--color-border, #e5e7eb)',
              fontSize: 13,
              lineHeight: 1.6,
              color: 'var(--color-text-secondary, #6b7280)',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              maxHeight: 160,
              overflowY: 'auto',
            }}
          >
            {error.message}
          </pre>
          <button
            type="button"
            onClick={this.handleReload}
            style={{
              padding: '8px 20px',
              borderRadius: 8,
              border: 'none',
              background: 'var(--color-primary, #1677ff)',
              color: '#fff',
              fontSize: 14,
              cursor: 'pointer',
            }}
          >
            重新加载
          </button>
        </div>
      </div>
    )
  }
}

export default ErrorBoundary
