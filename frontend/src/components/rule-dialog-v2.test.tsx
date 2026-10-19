import { screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/utils'
import { RuleDialog } from './rule-dialog'
import type { Rule } from '@/types'

vi.mock('@/lib/api', () => ({ rules: { preview: vi.fn() } }))
vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({ user: { preferences: { currency_display: 'BRL', language: 'en' } }, token: 't', isLoading: false }),
}))

const rule = {
  id: 'r1', user_id: 'u', name: 'Not Uber', conditions_op: 'and', priority: 0, is_active: true,
  conditions: [
    { field: 'type', op: 'equals', value: 'debit' },
    { not: { op: 'or', conditions: [
      { field: 'description_key', op: 'starts_with', value: 'UBER' },
      { field: 'merchant_name', op: 'in', value: ['99', 'Cabify'] },
    ] } },
  ],
  actions: [{ op: 'clear_category', value: null }],
  scope_account_kind: null,
} as unknown as Rule

it('AC-3.3 edits and saves a negated nested group unchanged', async () => {
  const onSave = vi.fn()
  const { user } = renderWithProviders(
    <RuleDialog open onClose={vi.fn()} rule={rule} categories={[]} categoryGroups={[]} accounts={[]}
      payees={[]} onSave={onSave} loading={false} />,
  )
  expect(screen.getByText('None of this (NOT)')).toBeInTheDocument()
  expect(screen.getByDisplayValue('99, Cabify')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Save' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({
    conditions: rule.conditions,
    actions: [{ op: 'clear_category', value: null }],
    scope_account_kind: null,
    scope_account_id: null,
  }))
})

it('AC-3.2 offers card-only fields once the rule is scoped to credit cards', async () => {
  const { user } = renderWithProviders(
    <RuleDialog open onClose={vi.fn()} rule={null} categories={[]} categoryGroups={[]} accounts={[]}
      payees={[]} onSave={vi.fn()} loading={false} />,
  )
  const field = screen.getAllByRole('combobox', { name: 'Field' })[0]
  expect(field).not.toHaveTextContent('Installment number')
  await user.selectOptions(screen.getByLabelText('Applies to'), 'credit_card')
  expect(screen.getAllByRole('combobox', { name: 'Field' })[0]).toHaveTextContent('Installment number')
})
