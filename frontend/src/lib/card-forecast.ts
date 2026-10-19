/**
 * Card statements as a timeline and as limit usage (RFC toledo-features §6.4).
 *
 * Installment parcels are commitments — the bank already reserved them — so
 * they always count. Recurring charges can still be cancelled, so they are
 * a separate, dashed forecast and never consume the limit.
 */
import { addMonths, endOfMonth, parseISO, startOfMonth } from 'date-fns'
import type { CardStatement } from '@/types'

export type PastRange = 3 | 6
export type FutureRange = 3 | 6 | 12

export interface ForecastPoint {
  month: string
  x: number // due date timestamp
  state: CardStatement['state']
  posted: number
  installments: number
  recurring: number
  /** Posted rows + parcels: what the statement will be at least. */
  committed: number
  /** committed + expected recurring charges; null on closed statements
   *  except the last one, which anchors the dashed line. */
  forecast: number | null
}

const isClosed = (s: CardStatement) => s.state === 'closed' || s.state === 'closed_estimated'
const dueOf = (s: CardStatement) => parseISO((s.due_date ?? s.month) + 'T00:00:00')

export function forecastPoints(
  statements: CardStatement[],
  today: Date,
  past: PastRange,
  future: FutureRange,
): ForecastPoint[] {
  const from = startOfMonth(addMonths(today, -past))
  const to = endOfMonth(addMonths(today, future))
  const all = statements.map((s): ForecastPoint => {
    const total = Number(s.total)
    const closed = isClosed(s)
    const installments = closed ? 0 : Number(s.projected_installments)
    const recurring = closed ? 0 : Number(s.projected_recurring)
    const committed = total - recurring
    return {
      month: s.month,
      x: dueOf(s).getTime(),
      state: s.state,
      posted: committed - installments,
      installments,
      recurring,
      committed,
      forecast: closed ? null : total,
    }
  })
  const lastClosed = all.map(p => p.state === 'closed' || p.state === 'closed_estimated').lastIndexOf(true)
  if (lastClosed >= 0 && lastClosed < all.length - 1) all[lastClosed].forecast = all[lastClosed].committed
  return all.filter(p => p.x >= from.getTime() && p.x <= to.getTime())
}

export interface LimitPart {
  month: string
  amount: number
}

export interface LimitUsage {
  parts: LimitPart[]
  /** What the bank says is used beyond the statements we can detail. */
  unexplained: number
  used: number
  limit: number | null
}

/** How each statement weighs on the limit: what is still unpaid in closed
 *  bills, plus posted rows and parcels of the open and future ones. */
export function limitUsage(
  statements: CardStatement[],
  limit: number | null,
  availableCredit: number | null,
): LimitUsage {
  const parts: LimitPart[] = []
  for (const s of statements) {
    const amount = isClosed(s)
      ? (s.status === 'due' || s.status === 'overdue' ? Number(s.total) - Number(s.paid) : 0)
      : Number(s.total) - Number(s.projected_recurring)
    if (amount > 0.005) parts.push({ month: s.month, amount: Math.round(amount * 100) / 100 })
  }
  const detailed = parts.reduce((sum, p) => sum + p.amount, 0)
  const bankUsed = limit != null && availableCredit != null ? limit - availableCredit : null
  const unexplained = bankUsed != null && bankUsed - detailed > 0.5
    ? Math.round((bankUsed - detailed) * 100) / 100
    : 0
  return { parts, unexplained, used: Math.round((detailed + unexplained) * 100) / 100, limit }
}

export interface StatementAmounts {
  /** Already posted on the statement (the bank's total once closed). */
  real: number
  /** real + parcels still to come + expected recurring charges; null once closed. */
  forecast: number | null
  installments: number
  recurring: number
}

export function statementAmounts(s: CardStatement): StatementAmounts {
  const total = Number(s.total)
  if (isClosed(s)) return { real: total, forecast: null, installments: 0, recurring: 0 }
  const installments = Number(s.projected_installments)
  const recurring = Number(s.projected_recurring)
  return { real: total - installments - recurring, forecast: total, installments, recurring }
}

/** Available credit expected while `month` is the bill to pay, anchored on
 *  the bank's number: the earlier statements are paid by then (freeing what
 *  they hold) and `month`'s own expected recurring charges are on the card.
 *  Recurring charges of the statements in between are charged and paid, so
 *  they cancel out. Null for a month already closed. */
export function availableForecast(
  statements: CardStatement[],
  month: string,
  availableCredit: number | null,
  limit: number | null,
): number | null {
  const target = statements.find(s => s.month === month)
  if (availableCredit == null || !target || isClosed(target)) return null
  const freed = limitUsage(statements, limit, availableCredit).parts
    .filter(p => p.month < month)
    .reduce((sum, p) => sum + p.amount, 0)
  const value = availableCredit + freed - Number(target.projected_recurring)
  const capped = limit != null ? Math.min(limit, value) : value
  return Math.round(capped * 100) / 100
}
