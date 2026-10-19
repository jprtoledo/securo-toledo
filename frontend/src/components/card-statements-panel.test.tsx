import { screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/utils'
import { CardStatementsPanel } from './card-statements-panel'

const api = vi.hoisted(() => ({ statements: vi.fn() }))
vi.mock('@/lib/api', () => ({ accounts: api }))
vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'BRL', language: 'en' } }, token: 't', isLoading: false }),
}))

const base = {
  bill_id: null, dates_estimated: false, total_estimated: false, posted_total: 0, spent: 0, prepaid: 0,
  paid: 0, paid_at: null, payments: [], minimum_payment: null, finance_charges: 0, reconciliation: null,
  history: null, purchase_count: 0, projected_installments: 0, projected_installment_count: 0,
  projected_recurring: 0, projected_recurring_count: 0,
}

it('AC-4.7 shows one card per bill month with its state', async () => {
  api.statements.mockResolvedValue({
    account_id: 'a', today: '2026-10-17', open_month: '2026-11-01', closing_day: 25, due_day: 5,
    cycle_days_manual: false,
    resolution_stats: { truth_samples: 24, forecast_offset_months: 1, fallback_order: ['forecast', 'cycle', 'post_date'],
      methods: { forecast: { samples: 24, hits: 24 } } },
    statements: [
      { ...base, month: '2026-10-01', state: 'closed', status: 'paid', total: 100, paid_at: '2026-10-04',
        reconciliation: { computed: 99, diff: 1 }, closing_date: '2026-09-25', due_date: '2026-10-05' },
      { ...base, month: '2026-11-01', state: 'open', status: 'not_closed', total: 50, total_estimated: true,
        closing_date: '2026-10-25', due_date: '2026-11-05' },
      { ...base, month: '2026-12-01', state: 'future', status: 'not_closed', total: 30, total_estimated: true,
        projected_installments: 30, projected_installment_count: 1, closing_date: '2026-11-25', due_date: '2026-12-05' },
    ],
  })
  renderWithProviders(<CardStatementsPanel accountId="a" currency="BRL" />)
  expect(await screen.findByText('Closed')).toBeInTheDocument()
  expect(screen.getByText('Open')).toBeInTheDocument()
  expect(screen.getByText('Future')).toBeInTheDocument()
  expect(screen.getByText('1 installment to come')).toBeInTheDocument()
  expect(screen.getByText(/Differs from the bank's total/)).toBeInTheDocument()
  expect(screen.getByText(/learned from the bills/)).toBeInTheDocument()
})
