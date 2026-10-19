import { screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/utils'
import { InstallmentPlansPanel } from './installment-plans-panel'

const api = vi.hoisted(() => ({ installmentPlans: vi.fn(), confirmInstallmentPlan: vi.fn(), linkInstallmentPlan: vi.fn() }))
vi.mock('@/lib/api', () => ({ accounts: api, transactions: api }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'BRL', language: 'en' } }, token: 't', isLoading: false }),
}))

const parcel = (number: number, month: string, kind: string, extra = {}) => ({
  number, month, amount: 50, kind, transaction_id: kind === 'posted' ? `tx${number}` : null,
  pending: false, link: kind === 'posted' ? 'auto' : null, issue: null, ...extra,
})

it('AC-5.8 shows posted, missing and projected parcels and confirms a conflict', async () => {
  api.installmentPlans.mockResolvedValue({
    open_month: '2026-11-01',
    alerts: { conflicts: 1, missing: 1 },
    plans: [{
      id: 'p', source: 'detected', description: 'PNEUSTORE', purchase_date: '2026-08-14', total_installments: 10,
      installment_amount: 50, total_amount: 500, first_month: '2026-08-01', last_month: '2027-05-01',
      current_installment: 4, remaining: 300, active: true, conflicts: 1, missing: 1, category_id: null,
      installments: [
        parcel(1, '2026-08-01', 'posted'), parcel(2, '2026-09-01', 'posted'),
        parcel(3, '2026-10-01', 'missing'),
        parcel(4, '2026-11-01', 'posted', { link: 'conflict', issue: { code: 'month', expected: '2026-11-01' } }),
        ...[5, 6, 7, 8, 9, 10].map(n => parcel(n, `2027-0${n - 4}-01`, 'projected')),
      ],
    }],
  })
  api.confirmInstallmentPlan.mockResolvedValue({})
  const { user } = renderWithProviders(<InstallmentPlansPanel accountId="a" currency="BRL" />)
  expect(await screen.findByText('PNEUSTORE')).toBeInTheDocument()
  expect(screen.getByText(/1 parcel\(s\) out of sequence/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Confirm' }))
  expect(api.confirmInstallmentPlan).toHaveBeenCalledWith('tx4')
})
