import { describe, expect, it } from 'vitest'
import { availableForecast, forecastPoints, limitUsage, statementAmounts } from './card-forecast'
import type { CardStatement } from '@/types'

const st = (month: string, state: CardStatement['state'], total: number, extra: Partial<CardStatement> = {}) => ({
  month, state, status: state === 'closed' ? 'paid' : 'not_closed', due_date: month.slice(0, 8) + '08',
  total, paid: state === 'closed' ? total : 0, projected_installments: 0, projected_recurring: 0, ...extra,
}) as CardStatement

const statements = [
  st('2026-08-01', 'closed', 1756.79),
  st('2026-09-01', 'closed', 683.94),
  st('2026-10-01', 'closed', 1181.37),
  st('2026-11-01', 'open', 1452.11, { projected_installments: 877.3, projected_recurring: 12.9 }),
  st('2026-12-01', 'future', 737.77, { projected_installments: 724.87, projected_recurring: 12.9 }),
  st('2027-06-01', 'future', 114.83, { projected_installments: 101.93, projected_recurring: 12.9 }),
]
const today = new Date(2026, 9, 17)

describe('forecastPoints', () => {
  it('splits parcels (always counted) from recurring charges (dashed forecast)', () => {
    const points = forecastPoints(statements, today, 3, 3)
    expect(points.map(p => p.month)).toEqual(['2026-08-01', '2026-09-01', '2026-10-01', '2026-11-01', '2026-12-01'])
    const nov = points.find(p => p.month === '2026-11-01')!
    expect(nov.committed).toBeCloseTo(1439.21)
    expect(nov.installments).toBeCloseTo(877.3)
    expect(nov.posted).toBeCloseTo(561.91)
    expect(nov.forecast).toBeCloseTo(1452.11)
    // The dashed line starts from the last closed statement.
    expect(points.find(p => p.month === '2026-10-01')!.forecast).toBeCloseTo(1181.37)
    expect(points.find(p => p.month === '2026-09-01')!.forecast).toBeNull()
  })

  it('windows by months around today', () => {
    expect(forecastPoints(statements, today, 6, 12).map(p => p.month)).toContain('2027-06-01')
    expect(forecastPoints(statements, today, 3, 6).map(p => p.month)).not.toContain('2027-06-01')
  })
})

describe('limitUsage', () => {
  it('sums unpaid and future commitments per month, without recurring charges', () => {
    const usage = limitUsage(statements, 4600, 2400)
    expect(usage.parts).toEqual([
      { month: '2026-11-01', amount: 1439.21 },
      { month: '2026-12-01', amount: 724.87 },
      { month: '2027-06-01', amount: 101.93 },
    ])
    // The bank reports 2200 used; 2266.01 detailed is already above it.
    expect(usage.unexplained).toBe(0)
    expect(usage.used).toBeCloseTo(2266.01)
  })

  it('keeps an unpaid closed bill and what the bank uses beyond the statements', () => {
    const unpaid = [st('2026-10-01', 'closed', 500, { status: 'due', paid: 100 }), ...statements.slice(3)]
    const usage = limitUsage(unpaid, 4600, 1000)
    expect(usage.parts[0]).toEqual({ month: '2026-10-01', amount: 400 })
    expect(usage.unexplained).toBeCloseTo(3600 - (400 + 1439.21 + 724.87 + 101.93))
    expect(usage.used).toBeCloseTo(3600)
  })
})

describe('statementAmounts', () => {
  it('splits what is posted from what is still expected', () => {
    const nov = statementAmounts(statements[3])
    expect(nov.real).toBeCloseTo(561.91)
    expect(nov.forecast).toBeCloseTo(1452.11)
    expect(statementAmounts(statements[2])).toEqual({ real: 1181.37, forecast: null, installments: 0, recurring: 0 })
  })
})

describe('availableForecast', () => {
  it('frees the earlier statements and takes the month’s recurring charges', () => {
    // Open month: the bank number minus what recurs before it closes.
    expect(availableForecast(statements, '2026-11-01', 38.23, 4600)).toBeCloseTo(25.33)
    // December: November paid (1439.21 freed), December's recurring on the card.
    expect(availableForecast(statements, '2026-12-01', 38.23, 4600)).toBeCloseTo(38.23 + 1439.21 - 12.9)
    expect(availableForecast(statements, '2026-10-01', 38.23, 4600)).toBeNull()
    expect(availableForecast(statements, '2026-12-01', null, 4600)).toBeNull()
  })

  it('never exceeds the limit', () => {
    expect(availableForecast(statements, '2027-06-01', 4500, 4600)).toBe(4600)
  })
})
