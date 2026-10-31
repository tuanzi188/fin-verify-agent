import { describe, it, expect } from 'vitest'
import { deriveStatus, normalizeStatus, STATUS_META } from '../utils/status'

describe('status vocabulary', () => {
  it('uses the five agreed labels', () => {
    expect(STATUS_META.answered.label).toBe('已回答')
    expect(STATUS_META.abstained.label).toBe('资料不足')
    expect(STATUS_META.clarified.label).toBe('需澄清')
    expect(STATUS_META.needs_review.label).toBe('需人工复核')
    expect(STATUS_META.error.label).toBe('执行失败')
  })

  it('normalizes unknown backend status to error', () => {
    expect(normalizeStatus('weird')).toBe('error')
    expect(normalizeStatus(undefined)).toBe('error')
  })

  it('promotes answered to needs_review when corpus changed', () => {
    expect(deriveStatus('answered', { corpusChanged: true })).toBe('needs_review')
  })

  it('promotes answered to needs_review when there are claims but no evidence', () => {
    expect(deriveStatus('answered', { hasClaims: true, hasCitations: false })).toBe('needs_review')
    expect(deriveStatus('answered', { hasClaims: true, hasCitations: true })).toBe('answered')
  })

  it('keeps terminal states untouched', () => {
    expect(deriveStatus('abstained', { corpusChanged: true })).toBe('abstained')
    expect(deriveStatus('error', { hasClaims: true })).toBe('error')
    expect(deriveStatus('clarified')).toBe('clarified')
  })
})
