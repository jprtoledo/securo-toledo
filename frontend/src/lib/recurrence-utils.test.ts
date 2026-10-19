import { describe, expect, it } from 'vitest'
import { billedUnits, weekdaysInMonth } from './recurrence-utils'

describe('weekly billed monthly (AC-6.8)', () => {
  it('counts Tuesdays', () => {
    expect(weekdaysInMonth(2026, 9, 1)).toBe(5)
    expect(weekdaysInMonth(2026, 10, 1)).toBe(4)
  })

  it('bills the previous month by default', () => {
    expect(billedUnits(2026, 10, 1, 'previous_month').units).toBe(5)
    expect(billedUnits(2026, 11, 1, 'previous_month').units).toBe(4)
    expect(billedUnits(2026, 1, 1, 'previous_month')).toEqual({ units: 5, year: 2025, month: 12 })
    expect(billedUnits(2026, 10, 1, 'same_month').units).toBe(4)
  })
})
