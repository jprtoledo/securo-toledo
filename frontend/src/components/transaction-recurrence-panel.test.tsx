import { screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/utils'
import { TransactionRecurrencePanel } from './transaction-recurrence-panel'
import type { RecurringTransaction, Transaction } from '@/types'

const api = vi.hoisted(() => ({
  transactions: { linkRecurring: vi.fn(), reviewRecurring: vi.fn(), confirmRecurring: vi.fn() },
  recurring: { list: vi.fn() },
}))
vi.mock('@/lib/api', () => ({ transactions: api.transactions, recurring: api.recurring }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const netflix = {
  id: 'rec-1', description: 'Netflix', account_id: 'card', is_active: true, frequency: 'monthly',
} as RecurringTransaction
const spotify = {
  id: 'rec-2', description: 'Spotify', account_id: 'card', is_active: true, frequency: 'monthly',
} as RecurringTransaction

const conflict = {
  id: 'tx-1',
  account_id: 'card',
  description: 'NETFLIX.COM',
  recurring_transaction_id: 'rec-1',
  recurring_link: 'conflict',
  recurring_period: '2026-10-01',
  recurring_issue: { code: 'amount_changed', charged: '59.90', expected: '55.90' },
} as unknown as Transaction

beforeEach(() => {
  vi.resetAllMocks()
  api.recurring.list.mockResolvedValue([netflix, spotify])
})

it('AC-6.2 confirms a changed amount from a conflict', async () => {
  api.transactions.confirmRecurring.mockResolvedValue({ ...conflict, recurring_link: 'manual', recurring_issue: null })
  const { user } = renderWithProviders(<TransactionRecurrencePanel transaction={conflict} />)
  expect(await screen.findByText('Netflix')).toBeInTheDocument()
  expect(screen.getByText('Charged 59.90, expected 55.90: the amount changed.')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Confirm new amount' }))
  expect(api.transactions.confirmRecurring).toHaveBeenCalledWith('tx-1')
  expect(await screen.findByText('Linked by you')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Undo' })).toBeInTheDocument()
})

it('AC-6.3 / AC-6.4 accepts or rejects a conflict', async () => {
  api.transactions.reviewRecurring.mockResolvedValue({ ...conflict, recurring_transaction_id: null, recurring_link: null, recurring_issue: null })
  const { user } = renderWithProviders(<TransactionRecurrencePanel transaction={conflict} />)
  await screen.findByText('Netflix')
  expect(screen.getByRole('button', { name: 'Accept' })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Not this recurrence' }))
  expect(api.transactions.reviewRecurring).toHaveBeenCalledWith('tx-1', 'reject')
  expect(await screen.findByText('Not linked to a recurring charge')).toBeInTheDocument()
})

it('AC-6.1 links an unlinked charge by hand or marks it as not recurring', async () => {
  const tx = { id: 'tx-2', account_id: 'card', description: 'NETFLIX.COM' } as Transaction
  api.transactions.linkRecurring.mockResolvedValue({ ...tx, recurring_manual_id: 'rec-2', recurring_transaction_id: 'rec-2', recurring_link: 'manual' })
  const { user } = renderWithProviders(<TransactionRecurrencePanel transaction={tx} />)
  expect(screen.getByRole('button', { name: 'Not recurring' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Make recurring' })).toBeInTheDocument()
  await user.selectOptions(await screen.findByRole('combobox', { name: 'Link to…' }), 'rec-2')
  expect(api.transactions.linkRecurring).toHaveBeenCalledWith('tx-2', 'rec-2')
  expect(await screen.findByText('Spotify')).toBeInTheDocument()
})

it('AC-6.15 offers nothing for an unlinked parcel or bill payment', () => {
  const parcel = { id: 'tx-3', account_id: 'card', installment_number: 3, total_installments: 10 } as Transaction
  const { container, unmount } = renderWithProviders(<TransactionRecurrencePanel transaction={parcel} />)
  expect(container).toBeEmptyDOMElement()
  unmount()
  const payment = { id: 'tx-4', account_id: 'card', card_line_kind: 'payment' } as Transaction
  const second = renderWithProviders(<TransactionRecurrencePanel transaction={payment} />)
  expect(second.container).toBeEmptyDOMElement()
})
