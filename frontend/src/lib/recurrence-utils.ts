/** Weekly-billed-monthly helpers (RFC toledo-features §8.3), mirroring the backend. */

/** Occurrences of `weekday` (0 = Monday) in the month of `isoMonth` (YYYY-MM…). */
export function weekdaysInMonth(year: number, month: number, weekday: number): number {
  const days = new Date(Date.UTC(year, month, 0)).getUTCDate()
  let count = 0
  for (let d = 1; d <= days; d++) {
    const jsDay = new Date(Date.UTC(year, month - 1, d)).getUTCDay() // 0 = Sunday
    if ((jsDay + 6) % 7 === weekday) count++
  }
  return count
}

/** Units charged in the period billed in `year`/`month`: the weekday count of
 *  that month (same_month) or of the previous one (previous_month). */
export function billedUnits(
  year: number,
  month: number,
  weekday: number,
  basis: 'previous_month' | 'same_month',
): { units: number; year: number; month: number } {
  let y = year
  let m = month
  if (basis === 'previous_month') {
    m -= 1
    if (m === 0) { m = 12; y -= 1 }
  }
  return { units: weekdaysInMonth(y, m, weekday), year: y, month: m }
}
