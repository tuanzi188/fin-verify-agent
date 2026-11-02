import React from 'react'
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import CalcCard from '../components/CalcCard'

describe('计算结果与原始输入核验', () => {
  it('百分点展示换算后的值并说明复算尺度', () => {
    render(<CalcCard index={0} calc={{ formula: 'a-b', inputs: { a: '30%', b: '20%' }, result: '10', unit: 'pp', result_scale: '100' }} />)
    expect(screen.getByText('= 10 pp')).toBeTruthy()
    expect(screen.getByText('展示结果 = 公式原始结果 × 100')).toBeTruthy()
    expect(screen.queryByText('= 0.1 pp')).toBeNull()
  })

  it('将计算输入和对应来源坐标一起显示', () => {
    render(<CalcCard index={0} calc={{ formula: 'a-b', inputs: { a: '12.34', b: '10' }, result: '2.34', unit: '千元', input_sources: { a: { evidence_id: 'doc#t1#0#1', marker: 2, row: 0, column: 1 } } }} />)
    expect(screen.getByText(/来源 \[2\] 行 0 列 1/)).toBeTruthy()
    expect(screen.getByText('= 2.34 千元')).toBeTruthy()
  })
})
