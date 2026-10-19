/**
 * Pluggy cards read their months from the statements engine (RFC
 * toledo-features §6.4): the month view's bill total, rows and projections
 * must be the same numbers the Statements tab shows.
 */
import { beforeEach, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'

import AccountDetailPage from '@/pages/account-detail'
import { renderWithProviders } from '@/test/utils'

const api = vi.hoisted(() => ({
  accounts: {
    get: vi.fn(), bills: vi.fn(), summary: vi.fn(), list: vi.fn(), update: vi.fn(),
    statements: vi.fn(), installmentPlans: vi.fn(),
  },
  transactions: { list: vi.fn(), update: vi.fn(), delete: vi.fn(), create: vi.fn() },
  dashboard: { projectedTransactions: vi.fn() },
  categories: { list: vi.fn() },
  categoryGroups: { list: vi.fn() },
}))

vi.mock('@/lib/api', () => api)
vi.mock('@/hooks/use-display-locale', () => ({ useDisplayLocale: () => 'en-US', useDateLocale: () => 'en-US' }))
vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value, privacyMode: false, MASK: '***' }),
}))
vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { preferences: { currency_display: 'BRL' } } }) }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => ({ canWrite: true }) }))

const account = {
  id: 'card', name: 'CAIXA', type: 'credit_card', currency: 'BRL', balance: -1452.11, is_active: true,
  credit_limit: 5000, statement_close_day: 28, payment_due_day: 8, connection_id: 'conn',
}

const statement = (month: string, closing: string, due: string, state: string, total: number, items: unknown[] = []) => ({
  month, state, status: state === 'closed' ? 'paid' : 'not_closed', bill_id: state === 'closed' ? 'b' : null,
  closing_date: closing, due_date: due, dates_estimated: state !== 'closed', total, total_estimated: state !== 'closed',
  posted_total: total, spent: total, prepaid: 0, paid: 0, paid_at: null, payments: [], minimum_payment: null,
  finance_charges: 0, reconciliation: null, history: null, purchase_count: 1, projected_installments: 0,
  projected_installment_count: 0, projected_recurring: 0, projected_recurring_count: 0, projected_items: items,
})

const statements = {
  account_id: 'card', today: '2026-10-17', open_month: '2026-11-01', closing_day: 28, due_day: 8,
  cycle_days_manual: false, resolution_stats: null, reference_months: true,
  statements: [
    statement('2026-10-01', '2026-09-28', '2026-10-08', 'closed', 1181.37),
    { ...statement('2026-11-01', '2026-10-28', '2026-11-08', 'open', 1452.11, [
      { source: 'installment', amount: '120.00', description: 'PNEUSTORE', number: 3, total: 10, ref_id: 'plan-1', category_id: null },
      { source: 'recurrence', amount: '12.90', description: 'Clube iFood', ref_id: 'rec-1', category_id: null, date: '2026-10-25' },
    ]), projected_installments: 120, projected_recurring: 12.9 },
    { ...statement('2026-12-01', '2026-11-28', '2026-12-08', 'future', 120, [
      { source: 'installment', amount: '120.00', description: 'PNEUSTORE', number: 4, total: 10, ref_id: 'plan-1', category_id: null },
    ]), projected_installments: 120 },
  ],
}

function txCalls() {
  return api.transactions.list.mock.calls.map(([args]) => args as Record<string, unknown>)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.setSystemTime(new Date('2026-10-17T12:00:00'))
  api.accounts.get.mockResolvedValue(account)
  api.accounts.bills.mockResolvedValue([])
  api.accounts.list.mockResolvedValue([account])
  api.accounts.statements.mockResolvedValue(statements)
  api.accounts.installmentPlans.mockResolvedValue({ open_month: '2026-11-01', alerts: { conflicts: 0, missing: 0 }, plans: [] })
  api.accounts.summary.mockResolvedValue({ monthly_income: 0, monthly_expenses: 999, projected_income: 0, projected_expenses: 999 })
  api.transactions.list.mockResolvedValue({ items: [], total: 0 })
  api.dashboard.projectedTransactions.mockResolvedValue([])
  api.categories.list.mockResolvedValue([])
  api.categoryGroups.list.mockResolvedValue([])
})

function billTotalCard() {
  return screen.getByTestId('bill-total-card')
}

it('AC-4 the month view shows the statement total, rows by reference month and projections', async () => {
  const { user } = renderWithProviders(<AccountDetailPage />, { route: '/accounts/card', path: '/accounts/:id' })

  // Opens on the open statement, rows fetched by reference month.
  await waitFor(() => expect(txCalls().at(-1)).toEqual({ account_id: 'card', statement_month: '2026-11-01', limit: 500 }))
  await waitFor(() => expect(within(billTotalCard()).getByText(/1,452\.11/)).toBeInTheDocument())
  expect(screen.getAllByText('PNEUSTORE').length).toBeGreaterThan(0)
  expect(screen.getAllByText('3/10').length).toBeGreaterThan(0)
  expect(screen.getAllByText('Clube iFood').length).toBeGreaterThan(0)

  // Future months keep their projected parcels.
  await user.click(screen.getByTitle('Next cycle'))
  await waitFor(() => expect(txCalls().at(-1)!.statement_month).toBe('2026-12-01'))
  await waitFor(() => expect(within(billTotalCard()).getByText(/120\.00/)).toBeInTheDocument())
  expect(screen.getAllByText('4/10').length).toBeGreaterThan(0)

  // Closed months show the bank's total.
  await user.click(screen.getByTitle('Previous cycle'))
  await user.click(screen.getByTitle('Previous cycle'))
  await waitFor(() => expect(txCalls().at(-1)!.statement_month).toBe('2026-10-01'))
  await waitFor(() => expect(within(billTotalCard()).getByText(/1,181\.37/)).toBeInTheDocument())

  // The legacy date-window summary is never what the totals come from.
  expect(api.accounts.summary).not.toHaveBeenCalled()
})

it('replaces the bar strip with the statements line chart and its period filters', async () => {
  const { user } = renderWithProviders(<AccountDetailPage />, { route: '/accounts/card', path: '/accounts/:id' })
  const chart = await screen.findByTestId('card-forecast-chart')
  expect(within(chart).getByRole('button', { name: '−3m' })).toHaveAttribute('aria-pressed', 'true')
  expect(within(chart).getByRole('button', { name: '+6m' })).toHaveAttribute('aria-pressed', 'true')
  await user.click(within(chart).getByRole('button', { name: '+12m' }))
  expect(within(chart).getByRole('button', { name: '+12m' })).toHaveAttribute('aria-pressed', 'true')
  expect(within(chart).getByText('Posted + installments')).toBeInTheDocument()
  expect(within(chart).getByText('With expected recurring charges')).toBeInTheDocument()
})

it('splits limit usage by month, highlights the selected one and selects on click', async () => {
  api.accounts.get.mockResolvedValue({ ...account, available_credit: 2000 })
  const { user } = renderWithProviders(<AccountDetailPage />, { route: '/accounts/card', path: '/accounts/:id' })
  const usage = await screen.findByTestId('card-limit-usage')
  // Open November (1452.11 − 12.90 recurring) and December's parcel; the
  // paid October bill reserves nothing. The bank reports 3000 used.
  const chips = within(usage).getAllByRole('button', { pressed: false })
    .concat(within(usage).getAllByRole('button', { pressed: true }))
  expect(chips.map(c => c.textContent)).toEqual(expect.arrayContaining([
    expect.stringMatching(/1,439\.21/), expect.stringMatching(/120\.00/),
  ]))
  const selected = within(usage).getByRole('button', { pressed: true })
  expect(selected).toHaveTextContent(/1,439\.21/)
  expect(within(usage).getByText('Not itemized')).toBeInTheDocument()
  expect(within(usage).getByText(/3,000\.00 \/ R\$\s?5,000\.00/)).toBeInTheDocument()

  await user.click(within(usage).getByRole('button', { name: /120\.00/, pressed: false }))
  await waitFor(() => expect(txCalls().at(-1)!.statement_month).toBe('2026-12-01'))
  await waitFor(() => expect(within(screen.getByTestId('card-limit-usage')).getByRole('button', { pressed: true })).toHaveTextContent(/120\.00/))
})

it('shows the real and the forecast bill total and available credit', async () => {
  api.accounts.get.mockResolvedValue({ ...account, available_credit: 100 })
  const { user } = renderWithProviders(<AccountDetailPage />, { route: '/accounts/card', path: '/accounts/:id' })
  // November: 1452.11 − 120 parcels − 12.90 recurring already posted.
  await waitFor(() => expect(within(billTotalCard()).getByText(/1,319\.21/)).toBeInTheDocument())
  expect(within(billTotalCard()).getByText('Real')).toBeInTheDocument()
  expect(billTotalCard()).toHaveTextContent(/Forecast\s*R\$\s?1,452\.11/)
  // Open month: the bank's 100 minus the recurring charge still to come.
  expect(screen.getByTestId('available-forecast')).toHaveTextContent(/87\.10/)

  // December: November paid frees 1452.11 − 12.90.
  await user.click(screen.getByTitle('Next cycle'))
  await waitFor(() => expect(screen.getByTestId('available-forecast')).toHaveTextContent(/1,539\.21/))

  // A closed bill has only its real total.
  await user.click(screen.getByTitle('Previous cycle'))
  await user.click(screen.getByTitle('Previous cycle'))
  await waitFor(() => expect(within(billTotalCard()).getByText(/1,181\.37/)).toBeInTheDocument())
  expect(within(billTotalCard()).queryByText(/Forecast/)).not.toBeInTheDocument()
  expect(screen.queryByTestId('available-forecast')).not.toBeInTheDocument()
})
