import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, screen, waitFor } from '@testing-library/react'

import ReportsPage from '@/pages/reports'
import { renderWithProviders, t } from '@/test/utils'
import type { ReportResponse } from '@/types'

const api = vi.hoisted(() => {
  // One mock per report kind behind the page's single `reports.window` call.
  const kinds = { net_worth: vi.fn(), income_expenses: vi.fn(), cash_flow: vi.fn() }
  return {
    kinds,
    reports: {
      window: vi.fn((kind: keyof typeof kinds, opts: unknown) => kinds[kind](opts)),
      netWorth: kinds.net_worth,
      incomeExpenses: kinds.income_expenses,
      cashFlow: kinds.cash_flow,
    },
  // The page reads the workspace timezone to tell a balance that is already
  // negative from one that crosses later. Unresolved here, which lands on the
  // browser's own day.
    timezones: { list: vi.fn().mockResolvedValue({ timezones: [], default: null }) },
  }
})

vi.mock('@/lib/api', () => ({ reports: api.reports, timezones: api.timezones }))

vi.mock('@/hooks/use-display-locale', () => ({
  useDisplayLocale: () => 'en-US',
  useDateLocale: () => 'en-US',
}))

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'USD' } } }),
}))

vi.mock('@/contexts/collection-filter-context', () => ({
  useCollectionFilter: () => ({ activeAccountIds: null, activeWalletIds: null }),
}))

vi.mock('@/hooks/use-privacy-mode', () => ({
  usePrivacyMode: () => ({ mask: (value: string) => value, privacyMode: false, MASK: '••••' }),
}))

function emptyReport(type: string): ReportResponse {
  return {
    summary: { primary_value: 1000, change_amount: 100, change_percent: 5, breakdowns: [] },
    trend: [{ date: '2026-01-01', value: 1000, breakdowns: {}, change: null }],
    meta: { type, series_keys: [], currency: 'USD', interval: 'monthly' },
    composition: [],
    category_trend: [],
  }
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(2026, 8, 12, 12))
  vi.clearAllMocks()
  api.reports.window.mockImplementation((kind: keyof typeof api.kinds, opts: unknown) => api.kinds[kind](opts))
  api.reports.netWorth.mockResolvedValue(emptyReport('net_worth'))
  api.reports.incomeExpenses.mockResolvedValue(emptyReport('income_expenses'))
  api.reports.cashFlow.mockResolvedValue(emptyReport('cash_flow'))
})

afterEach(() => vi.useRealTimers())

describe('Reports page — reference date and period', () => {
  it('asks every tab for the months before and after the reference date', async () => {
    const { user } = renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.window).toHaveBeenCalledWith(
      'net_worth',
      expect.objectContaining({ referenceDate: '2026-09-12', pastMonths: 6, futureMonths: 3, interval: 'monthly' }),
    ))
    expect(screen.getByRole('button', { name: '−6m' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '+3m' })).toHaveAttribute('aria-pressed', 'true')

    // The two sides are chosen separately.
    await user.click(screen.getByRole('button', { name: '−3m' }))
    await user.click(screen.getByRole('button', { name: '+12m' }))
    await waitFor(() => expect(api.reports.window).toHaveBeenLastCalledWith(
      'net_worth', expect.objectContaining({ pastMonths: 3, futureMonths: 12 }),
    ))
    // Picking a side's period again turns that side off…
    await user.click(screen.getByRole('button', { name: '+12m' }))
    await waitFor(() => expect(api.reports.window).toHaveBeenLastCalledWith(
      'net_worth', expect.objectContaining({ pastMonths: 3, futureMonths: 0 }),
    ))
    // …but never both.
    await user.click(screen.getByRole('button', { name: '−3m' }))
    expect(screen.getByRole('button', { name: '−3m' })).toHaveAttribute('aria-pressed', 'true')

    await user.click(screen.getByRole('button', { name: t('reports.cashFlow') }))
    await waitFor(() => expect(api.reports.window).toHaveBeenLastCalledWith(
      'cash_flow', expect.objectContaining({ referenceDate: '2026-09-12', pastMonths: 3, futureMonths: 0 }),
    ))
    await user.click(screen.getByRole('button', { name: t('reports.moneyMap') }))
    await waitFor(() => expect(api.reports.window).toHaveBeenLastCalledWith(
      'income_expenses', expect.objectContaining({ pastMonths: 3 }),
    ))
  })

  it('draws one forecast line, with no historical estimate', async () => {
    const report = emptyReport('net_worth')
    api.reports.netWorth.mockResolvedValue({
      ...report,
      trend: [
        { date: '2026-09', value: 1000, breakdowns: {}, change: null },
        { date: '2026-10', value: 1200, breakdowns: {}, change: 200 },
      ],
      meta: { ...report.meta, forecast_start_date: '2026-09' },
    })
    renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalled())
    expect(screen.queryByText(t('reports.withEstimate'))).not.toBeInTheDocument()
  })
})

describe('Reports page — query failure', () => {
  it('surfaces a rejected request instead of showing stale numbers', async () => {
    const { user } = renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalledTimes(1))

    api.reports.netWorth.mockRejectedValueOnce({ response: { data: { detail: 'bad window' } } })
    await user.click(screen.getByRole('button', { name: '+6m' }))

    expect(await screen.findByText('bad window')).toBeInTheDocument()
    // The prior successful report must not linger under the error.
    expect(screen.queryByText(t('reports.trend'), { exact: false })).not.toBeInTheDocument()
    // Tabs and period controls stay usable so the user can correct the request.
    expect(screen.getByRole('button', { name: '−3m' })).toBeInTheDocument()
  })

  it('shows a generic fallback for a network failure and recovers via Retry', async () => {
    const { user } = renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalledTimes(1))

    api.reports.netWorth.mockRejectedValueOnce(new Error('network error'))
    await user.click(screen.getByRole('button', { name: '−3m' }))

    expect(await screen.findByText(t('reports.loadError'))).toBeInTheDocument()

    api.reports.netWorth.mockResolvedValueOnce(emptyReport('net_worth'))
    await user.click(screen.getByRole('button', { name: t('common.retry') }))

    await waitFor(() => expect(screen.queryByText(t('reports.loadError'))).not.toBeInTheDocument())
  })

  it('recovers by changing the period after a rejected selection', async () => {
    const { user } = renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalledTimes(1))

    api.reports.netWorth.mockRejectedValueOnce({ response: { data: { detail: 'bad window' } } })
    await user.click(screen.getByRole('button', { name: '+12m' }))
    expect(await screen.findByText('bad window')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '−3m' }))
    await waitFor(() => expect(screen.queryByText('bad window')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: '−3m' })).toHaveAttribute('aria-pressed', 'true')
  })
})

describe('Reports page, cash-flow runway', () => {
  async function openCashFlow(meta: Partial<ReportResponse['meta']>) {
    const report = emptyReport('cash_flow')
    api.reports.cashFlow.mockResolvedValue({ ...report, meta: { ...report.meta, ...meta } })
    const { user } = renderWithProviders(<ReportsPage />)
    await waitFor(() => expect(api.reports.netWorth).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: t('reports.cashFlow') }))
    return screen.findByTestId('cash-flow-runway')
  }

  it('names the day the balance goes negative and its lowest point', async () => {
    const callout = await openCashFlow({
      runway_date: '2099-10-05',
      lowest_balance: -500,
      lowest_balance_date: '2099-12-20',
    })

    expect(callout).toHaveTextContent(t('reports.runwayNegativeOn', { date: 'Oct 5, 2099' }))
    expect(callout).toHaveTextContent('-$500.00')
    // Only the crossing date is shown: a second date next to the amount reads
    // as part of the same sentence rather than as the bottom of the dip.
    expect(callout).not.toHaveTextContent('Dec 20')
  })

  it('reads today in the workspace timezone, not the browser one', async () => {
    // 23:30 UTC on the 13th is already the 14th fourteen hours ahead, so the
    // same crossing date is "already negative" there and still ahead in UTC.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date('2026-10-13T23:30:00Z'))
    try {
      api.timezones.list.mockResolvedValue({ timezones: [], default: 'Pacific/Kiritimati' })
      const ahead = await openCashFlow({
        runway_date: '2026-10-14',
        lowest_balance: -10,
        lowest_balance_date: '2026-10-14',
      })
      await waitFor(() => expect(ahead).toHaveTextContent(t('reports.runwayAlreadyNegative')))

      cleanup()
      api.timezones.list.mockResolvedValue({ timezones: [], default: 'UTC' })
      const behind = await openCashFlow({
        runway_date: '2026-10-14',
        lowest_balance: -10,
        lowest_balance_date: '2026-10-14',
      })
      await waitFor(() =>
        expect(behind).toHaveTextContent(t('reports.runwayNegativeOn', { date: 'Oct 14' })),
      )
    } finally {
      vi.useRealTimers()
      api.timezones.list.mockResolvedValue({ timezones: [], default: null })
    }
  })

  it('says the balance is already negative rather than naming today', async () => {
    const callout = await openCashFlow({
      runway_date: '2020-01-02',
      lowest_balance: -500,
      lowest_balance_date: '2020-01-31',
    })

    expect(callout).toHaveTextContent(t('reports.runwayAlreadyNegative'))
    expect(callout).not.toHaveTextContent(t('reports.runwayNegativeOn', { date: 'Jan 2' }))
  })

  it.each([
    [400, '$400.00'],
    [0, '$0.00'],
  ])('reports a lowest balance of %d as never going below zero', async (lowest, amount) => {
    const callout = await openCashFlow({
      runway_date: null,
      lowest_balance: lowest,
      lowest_balance_date: '2026-12-01',
    })

    expect(callout).toHaveTextContent(t('reports.lowestBalance'))
    expect(callout).toHaveTextContent(amount)
    expect(callout).toHaveTextContent('Dec 1')
  })

  it('is not shown on the other tabs', async () => {
    api.reports.netWorth.mockResolvedValue({
      ...emptyReport('net_worth'),
      meta: { ...emptyReport('net_worth').meta, lowest_balance: 1, lowest_balance_date: '2026-10-01' },
    })
    renderWithProviders(<ReportsPage />)
    await screen.findAllByText('$1,000.00')

    expect(screen.queryByTestId('cash-flow-runway')).not.toBeInTheDocument()
  })
})
