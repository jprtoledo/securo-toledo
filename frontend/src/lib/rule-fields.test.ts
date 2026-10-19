import { describe, expect, it } from 'vitest'
import {
  actionsForScope,
  fieldsForScope,
  isBlankConditionValue,
  opsForField,
  parseListValue,
} from './rule-fields'
import { flattenConditions, nodeDepth } from './rule-conditions'
import type { RuleConditionNode } from '../types'

describe('rule field catalog (RFC §5.3)', () => {
  it('offers card-only fields and actions only to card-scoped rules (AC-3.2)', () => {
    const all = fieldsForScope(null).map(f => f.value)
    const card = fieldsForScope('credit_card').map(f => f.value)
    expect(all).not.toContain('installment_number')
    expect(card).toContain('installment_number')
    expect(actionsForScope('bank').map(a => a.value)).not.toContain('set_card_line_kind')
    expect(actionsForScope('credit_card').map(a => a.value)).toContain('strip_installment_marker')
  })

  it('limits enum fields to equality, list and emptiness operators', () => {
    expect(opsForField('operation_type').map(o => o.value)).toEqual([
      'equals', 'not_equals', 'in', 'not_in', 'is_empty', 'is_not_empty',
    ])
    expect(opsForField('description').map(o => o.value)).toContain('not_regex')
  })

  it('knows when a value is blank', () => {
    expect(isBlankConditionValue('is_empty', undefined)).toBe(false)
    expect(isBlankConditionValue('in', [])).toBe(true)
    expect(isBlankConditionValue('in', ['99'])).toBe(false)
    expect(isBlankConditionValue('contains', ' ')).toBe(true)
    expect(parseListValue('99, Cabify ,')).toEqual(['99', 'Cabify'])
  })
})

describe('condition trees', () => {
  const tree: RuleConditionNode[] = [
    { field: 'type', op: 'equals', value: 'debit' },
    { not: { op: 'or', conditions: [
      { field: 'description_key', op: 'starts_with', value: 'UBER' },
      { field: 'merchant_name', op: 'in', value: ['99', 'Cabify'] },
    ] } },
  ]

  it('flattens leaves at any depth and measures depth', () => {
    expect(flattenConditions(tree).map(c => c.field)).toEqual(['type', 'description_key', 'merchant_name'])
    expect(nodeDepth(tree[1])).toBe(3)
  })
})
