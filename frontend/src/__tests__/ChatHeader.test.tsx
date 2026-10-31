import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

vi.mock('../api/client', () => ({
  getAuthenticatedProfile: vi.fn(),
  verifyAccessToken: vi.fn(),
  clearAccessToken: vi.fn(),
  extractErrorMessage: vi.fn(),
}))

import { getAuthenticatedProfile } from '../api/client'
import { UserProvider } from '../contexts/UserContext'
import ChatHeader from '../components/ChatHeader'

function renderHeader() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <UserProvider>
        <ChatHeader
          isDark={false}
          stats={null}
          connectionState="connected"
          onToggleTheme={() => {}}
          onReconnect={() => {}}
        />
      </UserProvider>
    </MemoryRouter>,
  )
}

describe('导航权限口径', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('普通登录用户可见实验台，但看不到审计', async () => {
    vi.mocked(getAuthenticatedProfile).mockResolvedValue({
      userId: 'alice',
      roles: ['user'],
      department: '研发',
    })
    renderHeader()
    await screen.findByText('alice')
    expect(screen.getByText('实验')).toBeInTheDocument()
    expect(screen.queryByText('审计')).not.toBeInTheDocument()
  })

  it('管理员可见审计', async () => {
    vi.mocked(getAuthenticatedProfile).mockResolvedValue({
      userId: 'operator',
      roles: ['admin'],
      department: '',
    })
    renderHeader()
    await screen.findByText('operator')
    expect(screen.getByText('实验')).toBeInTheDocument()
    expect(screen.getByText('审计')).toBeInTheDocument()
  })
})
