import { beforeEach, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import RecurringPage from '@/pages/recurring'
import { renderWithProviders } from '@/test/utils'
import type { RecurringTransaction } from '@/types'

const api = vi.hoisted(() => ({
  recurring: {
    list: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn(), generate: vi.fn(),
    end: vi.fn(), skip: vi.fn(), suggestions: vi.fn(), dismissSuggestion: vi.fn(),
  },
  categories: { list: vi.fn(), listIncludingHidden: vi.fn() },
  categoryGroups: { list: vi.fn() },
  accounts: { list: vi.fn() },
  currencies: { list: vi.fn() },
  transactions: { recurringDraft: vi.fn() },
}))
vi.mock('@/lib/api', () => api)
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/hooks/use-display-locale', () => ({ useDisplayLocale: () => 'en-US', useDateLocale: () => 'en-US' }))
vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: { preferences: { currency_display: 'BRL' } } }) }))
vi.mock('@/contexts/workspace-context', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useWorkspace: () => ({ canWrite: true }),
}))
vi.mock('@/hooks/use-privacy-mode', () => ({ usePrivacyMode: () => ({ mask: (value: string) => value }) }))

const slot = (period: string, status: string) => ({
  period, status, expected_date: period, month: period, amount: status === 'posted' ? '55.90' : null,
  expected_amount: '55.90', transaction_ids: [], conflict_ids: [], excluded: false,
})

const gym = {
  id: 'rec-1', description: 'Gym', amount: 99, currency: 'BRL', type: 'debit', frequency: 'monthly',
  weekend_adjustment: 'none', day_of_month: 5, start_date: '2026-01-05', end_date: null, is_active: true,
  auto_generate: false, next_occurrence: '2026-11-05', amount_primary: null, fx_rate_used: null,
  account_id: 'card', category_id: null, user_id: 'u',
  timeline: [slot('2026-07-01', 'posted'), slot('2026-08-01', 'conflict'), slot('2026-09-01', 'missed'), slot('2026-10-01', 'missed')],
  alerts: { conflicts: 1, last_missed: '2026-10-01', missed_streak: 2, suggest_end: true },
  next: slot('2026-11-01', 'expected'),
} as unknown as RecurringTransaction

const card = { id: 'card', name: 'Nubank', display_name: null, type: 'credit_card', is_closed: false }

beforeEach(() => {
  vi.resetAllMocks()
  api.recurring.list.mockResolvedValue([gym])
  api.categories.list.mockResolvedValue([])
  api.categoryGroups.list.mockResolvedValue([])
  api.accounts.list.mockResolvedValue([card])
  api.currencies.list.mockResolvedValue([{ code: 'BRL', symbol: 'R$', name: 'Real', flag: '' }])
  api.recurring.suggestions.mockResolvedValue([])
})

it('AC-6.11 / AC-6.12 shows the timeline, conflicts and the suggestion to end', async () => {
  api.recurring.end.mockResolvedValue({ ...gym, is_active: false })
  const { user } = renderWithProviders(<RecurringPage />)
  const timeline = await screen.findByTestId('recurrence-timeline')
  const statuses = Array.from(timeline.querySelectorAll('[data-status]')).map(el => el.getAttribute('data-status'))
  expect(statuses).toEqual(['posted', 'conflict', 'missed', 'missed'])
  expect(screen.getByTestId('recurring-conflicts')).toHaveTextContent('1 to review')
  const banner = screen.getByTestId('recurring-suggest-end')
  expect(banner).toHaveTextContent('Missed the last 2 periods. Has it ended?')
  await user.click(within(banner).getByRole('button', { name: 'End it' }))
  expect(api.recurring.end).toHaveBeenCalledWith('rec-1')
  // No auto-generating recurrence: the legacy generator button is hidden.
  expect(screen.queryByRole('button', { name: /Generate Pending/ })).not.toBeInTheDocument()
})

it('marks an occurrence as not happening, and undoes it', async () => {
  const upcoming = [slot('2026-07-01', 'posted'), slot('2099-01-01', 'expected'), slot('2099-02-01', 'skipped')]
  api.recurring.list.mockResolvedValue([{ ...gym, timeline: upcoming, alerts: { conflicts: 0, last_missed: null, missed_streak: 0, suggest_end: false } }])
  api.recurring.skip.mockResolvedValue(gym)
  const { user } = renderWithProviders(<RecurringPage />)
  const timeline = await screen.findByTestId('recurrence-timeline')
  // A posted occurrence has a charge: nothing to skip.
  expect(timeline.querySelector('button[data-status="posted"]')).toBeNull()

  await user.click(timeline.querySelector('button[data-status="expected"]') as HTMLElement)
  await user.click(await screen.findByRole('button', { name: "Won't happen" }))
  expect(api.recurring.skip).toHaveBeenCalledWith('rec-1', '2099-01-01', true)

  await user.click(timeline.querySelector('button[data-status="skipped"]') as HTMLElement)
  await user.click(await screen.findByRole('button', { name: 'It will happen' }))
  await waitFor(() => expect(api.recurring.skip).toHaveBeenLastCalledWith('rec-1', '2099-02-01', false))
})

it('AC-6.10 new recurrences are forecast, not materialized, and carry their condition', async () => {
  api.recurring.create.mockResolvedValue({ ...gym, link_stats: { recurrences: 1, linked: 3, recurring_conflicts: 0 } })
  const { user } = renderWithProviders(<RecurringPage />)
  await screen.findByText('Gym')
  await user.click(screen.getByRole('button', { name: /Add Recurring/ }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).queryByText('Generate transactions automatically')).not.toBeInTheDocument()
  const [description] = within(dialog).getAllByRole('textbox')
  await user.type(description, 'Spotify')
  await user.type(within(dialog).getAllByRole('spinbutton')[0], '21.90')
  await user.click(within(dialog).getByRole('button', { name: /Add condition/ }))
  const valueInput = within(dialog).getByPlaceholderText('Value...')
  await user.type(valueInput, 'SPOTIFY')
  await user.click(within(dialog).getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(api.recurring.create).toHaveBeenCalled())
  expect(api.recurring.create.mock.calls[0][0]).toMatchObject({
    description: 'Spotify',
    auto_generate: false,
    amount_mode: 'fixed',
    match_condition: { op: 'and', conditions: [{ field: 'description', op: 'contains', value: 'SPOTIFY' }] },
  })
})

it('lets a recurrence be paid from other accounts and names who paid each period', async () => {
  const bank = { id: 'bank', name: 'Sicredi', display_name: null, type: 'checking', is_closed: false }
  api.accounts.list.mockResolvedValue([card, bank])
  api.recurring.list.mockResolvedValue([{
    ...gym, extra_account_ids: ['bank'],
    timeline: [{ ...slot('2026-07-01', 'posted'), account_id: 'bank' }],
    alerts: { conflicts: 0, last_missed: null, missed_streak: 0, suggest_end: false },
  }])
  api.recurring.create.mockResolvedValue(gym)
  const { user } = renderWithProviders(<RecurringPage />)
  expect(await screen.findByTestId('recurring-accounts')).toHaveTextContent('2 accounts')
  const timeline = screen.getByTestId('recurrence-timeline')
  expect(timeline.querySelector('[data-status="posted"]')?.getAttribute('title')).toContain('paid from Sicredi')

  await user.click(screen.getByRole('button', { name: /Add Recurring/ }))
  const dialog = await screen.findByRole('dialog')
  await user.type(within(dialog).getAllByRole('textbox')[0], 'Psicóloga')
  await user.type(within(dialog).getAllByRole('spinbutton')[0], '380')
  // The form's account is the first one (Nubank): Sicredi is offered as an extra.
  await user.click(within(within(dialog).getByTestId('extra-accounts')).getByRole('checkbox', { name: 'Sicredi' }))
  await user.click(within(dialog).getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(api.recurring.create).toHaveBeenCalled())
  expect(api.recurring.create.mock.calls[0][0]).toMatchObject({ account_id: 'card', extra_account_ids: ['bank'] })
})

it('AC-6.8 previews the weekly billed monthly amount', async () => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(2026, 9, 17))
  try {
    const { user } = renderWithProviders(<RecurringPage />)
    await screen.findByText('Gym')
    await user.click(screen.getByRole('button', { name: /Add Recurring/ }))
    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(within(dialog).getByDisplayValue('Monthly'), 'weekly_billed_monthly')
    expect(within(dialog).getByText('Amount per week')).toBeInTheDocument()
    await user.type(within(dialog).getAllByRole('spinbutton')[0], '150')
    await user.selectOptions(within(dialog).getByDisplayValue('Monday'), '1')
    // Billed in October for September's five Tuesdays.
    expect(within(dialog).getByTestId('weekly-preview')).toHaveTextContent(/September: 5 × R\$\s?150\.00 = R\$\s?750\.00/)
  } finally {
    vi.useRealTimers()
  }
})

it('AC-6.13 lists suggestions per account and creates or dismisses them', async () => {
  api.recurring.suggestions.mockResolvedValue([{
    key: 'card:SPOTIFY', description: 'Spotify', type: 'debit', currency: 'BRL', amount_mode: 'fixed',
    amount: '21.90', amount_tolerance_pct: '0', frequency: 'monthly', day_of_month: 3, day_window: 3,
    start_date: '2026-06-03', account_id: 'card', periods_covered: 4,
    match_condition: { op: 'and', conditions: [{ field: 'description_key', op: 'starts_with', value: 'SPOTIFY' }] },
    occurrences: [{ id: 't1', date: '2026-10-03', amount: '21.90' }],
  }])
  const { user } = renderWithProviders(<RecurringPage />)
  const list = await screen.findByTestId('recurring-suggestions')
  expect(api.recurring.suggestions).toHaveBeenCalledWith('card')
  expect(within(list).getByText('Spotify')).toBeInTheDocument()
  expect(within(list).getByText(/seen in 4 periods/)).toBeInTheDocument()
  await user.click(within(list).getByRole('button', { name: /Dismiss/ }))
  expect(api.recurring.dismissSuggestion).toHaveBeenCalledWith('card', 'card:SPOTIFY')
  await user.click(within(list).getByRole('button', { name: 'Create' }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByDisplayValue('Spotify')).toBeInTheDocument()
  expect(within(dialog).getByDisplayValue('SPOTIFY')).toBeInTheDocument()
})

it('AC-6.15 "Make recurring" opens the form prefilled from the transaction', async () => {
  api.transactions.recurringDraft.mockResolvedValue({
    description: 'ACADEMIA', amount: '99.00', currency: 'BRL', type: 'debit', frequency: 'monthly',
    day_of_month: 5, start_date: '2026-10-05', account_id: 'card', amount_mode: 'fixed', auto_generate: false,
    match_condition: null,
  })
  renderWithProviders(<RecurringPage />, { route: '/recurring?fromTransaction=tx-9' })
  const dialog = await screen.findByRole('dialog')
  expect(api.transactions.recurringDraft).toHaveBeenCalledWith('tx-9')
  expect(within(dialog).getByDisplayValue('ACADEMIA')).toBeInTheDocument()
})

it('AC-6.14 legacy auto-generating recurrences keep their toggle and generator', async () => {
  api.recurring.list.mockResolvedValue([{ ...gym, auto_generate: true, timeline: null, alerts: null }])
  const { user } = renderWithProviders(<RecurringPage />)
  await screen.findByText('Gym')
  expect(screen.getByRole('button', { name: /Generate Pending/ })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Edit' }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByRole('checkbox', { name: /Generate transactions automatically/ })).toBeChecked()
})
