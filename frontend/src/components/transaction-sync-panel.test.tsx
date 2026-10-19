import { screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/utils'
import { TransactionSyncPanel } from './transaction-sync-panel'
import type { Transaction } from '@/types'

const api = vi.hoisted(() => ({ get: vi.fn(), markNotDuplicate: vi.fn(), setSyncLock: vi.fn() }))
vi.mock('@/lib/api', () => ({ transactions: api }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const base = {
  id: 'tx-1',
  description: 'PNEUSTORE 3/10',
  date: '2026-09-28',
  source: 'sync',
  is_ignored: true,
  ignored_reason: 'duplicate',
  duplicate_of_id: 'tx-posted',
  sync_locked: false,
} as unknown as Transaction

beforeEach(() => { vi.resetAllMocks() })

it('AC-2.11 shows the duplicate and lets the user say it is not one', async () => {
  api.get.mockResolvedValue({ id: 'tx-posted', description: 'PNEUSTORE 3/10', date: '2026-10-01' })
  api.markNotDuplicate.mockResolvedValue({ ...base, is_ignored: false, ignored_reason: null, duplicate_of_id: null })
  const onChanged = vi.fn()
  const { user } = renderWithProviders(<TransactionSyncPanel transaction={base} onChanged={onChanged} />)
  expect(await screen.findByText(/Duplicate of “PNEUSTORE 3\/10”/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Not a duplicate' }))
  expect(api.markNotDuplicate).toHaveBeenCalledWith('tx-1')
  expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ is_ignored: false }))
  expect(screen.queryByRole('button', { name: 'Not a duplicate' })).not.toBeInTheDocument()
})

it('AC-2.3 toggles the bank update lock', async () => {
  const tx = { ...base, duplicate_of_id: null, is_ignored: false, ignored_reason: null } as Transaction
  api.setSyncLock.mockResolvedValue({ ...tx, sync_locked: true })
  const { user } = renderWithProviders(<TransactionSyncPanel transaction={tx} />)
  await user.click(screen.getByRole('checkbox', { name: "Don't update from the bank" }))
  expect(api.setSyncLock).toHaveBeenCalledWith('tx-1', true)
  expect(screen.getByRole('checkbox', { name: "Don't update from the bank" })).toBeChecked()
})

it('AC-2.7 shows the removed-by-the-bank badge', () => {
  const tx = {
    ...base, duplicate_of_id: null, ignored_reason: 'provider_removed', provider_removed_at: '2026-10-16T10:00:00Z',
  } as Transaction
  renderWithProviders(<TransactionSyncPanel transaction={tx} />)
  expect(screen.getByText(/Removed by the bank on/)).toBeInTheDocument()
})
