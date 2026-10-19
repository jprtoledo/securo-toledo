import { describe, expect, it } from 'vitest'

import { shouldShowPendingBadge, statusFilterParams } from './transaction-status'

describe('shouldShowPendingBadge', () => {
  it('shows pending for transactions managed by Securo', () => {
    expect(shouldShowPendingBadge({ status: 'pending', source: 'manual' })).toBe(true)
    expect(shouldShowPendingBadge({ status: 'pending', source: 'recurring' })).toBe(true)
  })

  it('hides pending for bank-synced transactions', () => {
    expect(shouldShowPendingBadge({ status: 'pending', source: 'sync' })).toBe(false)
  })

  it('hides the badge for posted transactions', () => {
    expect(shouldShowPendingBadge({ status: 'posted', source: 'manual' })).toBe(false)
  })
})

describe('statusFilterParams', () => {
  it('sends posted/pending as status', () => {
    expect(statusFilterParams('pending')).toEqual({ status: 'pending' })
    expect(statusFilterParams('')).toEqual({})
  })

  it('sends the sync states as sync_state (RFC §4.9)', () => {
    expect(statusFilterParams('removed')).toEqual({ sync_state: 'removed' })
    expect(statusFilterParams('duplicate')).toEqual({ sync_state: 'duplicate' })
  })
})
