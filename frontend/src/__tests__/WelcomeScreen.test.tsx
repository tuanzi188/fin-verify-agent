import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'
import WelcomeScreen from '../components/WelcomeScreen'

describe('WelcomeScreen', () => {
  it('renders finance workbench empty state without documents', () => {
    render(<WelcomeScreen />)
    expect(screen.getAllByText('金融查证台').length).toBeGreaterThan(0)
    expect(screen.getByText(/本地还没有可查询的金融语料/)).toBeTruthy()
  })

  it('renders coverage numbers from the stats contract, not hardcoded chunks', () => {
    render(
      <WelcomeScreen
        hasDocuments
        stats={{
          documents: 7,
          companies: 3,
          periods: ['2025', '2024'],
          sections: 2842,
          tables: 1114,
          blocks: 5296,
          prose_blocks: 2363,
          table_blocks: 2933,
          corpus_ready: true,
          company_names: ['美的集团', '比亚迪', '贵州茅台'],
        }}
        onQuickQuestion={vi.fn()}
      />,
    )
    expect(screen.getByText('7')).toBeTruthy()
    expect(screen.getByText('2842')).toBeTruthy()
    expect(screen.getByText('1114')).toBeTruthy()
    expect(screen.getByText('5296')).toBeTruthy()
    expect(screen.getByText(/3 家公司/)).toBeTruthy()
  })
})
