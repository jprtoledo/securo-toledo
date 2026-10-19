import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { format } from 'date-fns'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { formatCurrency } from '@/lib/format'
import { resolveDateFnsLocale } from '@/lib/date-fns-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { cn } from '@/lib/utils'
import { forecastPoints, type ForecastPoint, type FutureRange, type PastRange } from '@/lib/card-forecast'
import type { CardStatement } from '@/types'

const CARD_COLOR = '#F43F5E'
const PAST: PastRange[] = [6, 3]
const FUTURE: FutureRange[] = [3, 6, 12]

interface Props {
  statements: CardStatement[]
  currency: string
  selectedMonth: string | null
  onSelect: (month: string) => void
  today?: Date
}

/** Statement totals over time (RFC toledo-features §6.4): a solid line for
 *  what is posted plus installment parcels — commitments the bank already
 *  reserved — and a dashed line adding expected recurring charges, which
 *  can still be cancelled. */
export function CardForecastChart({ statements, currency, selectedMonth, onSelect, today: todayProp }: Props) {
  const { t, i18n } = useTranslation()
  const locale = useDisplayLocale()
  const { mask, privacyMode } = usePrivacyMode()
  const [past, setPast] = useState<PastRange>(3)
  const [future, setFuture] = useState<FutureRange>(6)
  const dfLocale = resolveDateFnsLocale(i18n.resolvedLanguage ?? i18n.language)
  const today = useMemo(() => todayProp ?? new Date(), [todayProp])
  const todayTs = today.getTime()

  const points = useMemo(() => forecastPoints(statements, today, past, future), [statements, today, past, future])
  const selected = points.find(p => p.month === selectedMonth) ?? null
  const monthLabel = (month: string) => format(new Date(month + 'T00:00:00'), 'MMM yy', { locale: dfLocale })
  const money = (v: number) => mask(formatCurrency(v, currency, locale))
  const hasRecurring = points.some(p => p.recurring > 0)

  const chip = (active: boolean) => cn(
    'px-2.5 py-1 text-xs font-medium rounded-md transition-colors tabular-nums',
    active ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
  )

  return (
    <div className="bg-card rounded-xl border border-border shadow-sm p-3 sm:p-4 mb-6" data-testid="card-forecast-chart">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <svg width="18" height="4" aria-hidden><line x1="0" y1="2" x2="18" y2="2" stroke={CARD_COLOR} strokeWidth="2" /></svg>
            {t('accounts.forecast.committed')}
          </span>
          {hasRecurring && (
            <span className="flex items-center gap-1.5">
              <svg width="18" height="4" aria-hidden><line x1="0" y1="2" x2="18" y2="2" stroke={CARD_COLOR} strokeWidth="2" strokeDasharray="4 3" /></svg>
              {t('accounts.forecast.withRecurring')}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2" role="group" aria-label={t('accounts.forecast.range')}>
          <div className="flex items-center gap-0.5 bg-muted rounded-lg p-0.5">
            {PAST.map(n => (
              <button key={n} type="button" className={chip(past === n)} aria-pressed={past === n} onClick={() => setPast(n)}>
                −{n}m
              </button>
            ))}
          </div>
          <div className="flex items-center gap-0.5 bg-muted rounded-lg p-0.5">
            {FUTURE.map(n => (
              <button key={n} type="button" className={chip(future === n)} aria-pressed={future === n} onClick={() => setFuture(n)}>
                +{n}m
              </button>
            ))}
          </div>
        </div>
      </div>
      <div className="h-48">
        {points.length === 0 ? (
          <p className="text-muted-foreground text-sm text-center py-16">{t('dashboard.noData')}</p>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={points}
              margin={{ top: 12, right: 12, left: 0, bottom: 0 }}
              onClick={(state) => {
                const index = state?.activeTooltipIndex
                const point = index != null ? points[Number(index)] : undefined
                if (point) onSelect(point.month)
              }}
              style={{ cursor: 'pointer' }}
            >
              <CartesianGrid vertical={false} stroke="var(--border)" strokeOpacity={0.6} />
              <XAxis
                dataKey="x"
                type="number"
                scale="time"
                domain={['dataMin', 'dataMax']}
                ticks={points.map(p => p.x)}
                tickFormatter={(x: number) => {
                  const p = points.find(q => q.x === x)
                  return p ? monthLabel(p.month) : ''
                }}
                tick={{ fontSize: 10, fill: 'var(--muted-foreground)' }}
                axisLine={false}
                tickLine={false}
                interval="preserveStartEnd"
                minTickGap={12}
                padding={{ left: 12, right: 12 }}
              />
              <YAxis
                tickFormatter={(v: number) => privacyMode ? '' : v === 0 ? '0' : formatCurrency(v, currency, locale).replace(/[,.]00$/, '')}
                tick={{ fontSize: 10, fill: 'var(--muted-foreground)' }}
                axisLine={false}
                tickLine={false}
                width={56}
                tickCount={5}
              />
              <ReferenceLine
                x={todayTs}
                stroke="var(--muted-foreground)"
                strokeWidth={1}
                ifOverflow="extendDomain"
                label={{ value: t('accounts.forecast.today'), position: 'insideTopLeft', fontSize: 10, fill: 'var(--muted-foreground)' }}
              />
              <Tooltip
                cursor={{ stroke: 'var(--muted-foreground)', strokeWidth: 1 }}
                content={({ active, payload }) => {
                  const p = active ? (payload?.[0]?.payload as ForecastPoint | undefined) : undefined
                  if (!p) return null
                  const closed = p.state === 'closed' || p.state === 'closed_estimated'
                  return (
                    <div className="rounded-xl border border-border bg-card px-3 py-2 text-xs shadow-md space-y-1 min-w-[180px]">
                      <p className="font-medium text-muted-foreground capitalize">
                        {monthLabel(p.month)} · {t(`accounts.statements.state.${p.state}`)}
                      </p>
                      <p className="flex items-center justify-between gap-3">
                        <span className="flex items-center gap-1.5 text-muted-foreground">
                          <svg width="12" height="4" aria-hidden><line x1="0" y1="2" x2="12" y2="2" stroke={CARD_COLOR} strokeWidth="2" /></svg>
                          {t('accounts.forecast.committed')}
                        </span>
                        <span className="font-semibold tabular-nums text-foreground">{money(p.committed)}</span>
                      </p>
                      {!closed && (
                        <>
                          <p className="flex justify-between gap-3 pl-[18px] text-muted-foreground">
                            <span>{t('accounts.forecast.posted')}</span><span className="tabular-nums">{money(p.posted)}</span>
                          </p>
                          <p className="flex justify-between gap-3 pl-[18px] text-muted-foreground">
                            <span>{t('accounts.forecast.installments')}</span><span className="tabular-nums">{money(p.installments)}</span>
                          </p>
                        </>
                      )}
                      {!closed && p.recurring !== 0 && (
                        <p className="flex items-center justify-between gap-3">
                          <span className="flex items-center gap-1.5 text-muted-foreground">
                            <svg width="12" height="4" aria-hidden><line x1="0" y1="2" x2="12" y2="2" stroke={CARD_COLOR} strokeWidth="2" strokeDasharray="3 2" /></svg>
                            {t('accounts.forecast.withRecurring')}
                          </span>
                          <span className="font-semibold tabular-nums text-foreground">{money(p.committed + p.recurring)}</span>
                        </p>
                      )}
                    </div>
                  )
                }}
              />
              <Line
                type="linear"
                dataKey="forecast"
                stroke={CARD_COLOR}
                strokeWidth={2}
                strokeDasharray="5 4"
                dot={false}
                activeDot={false}
                connectNulls={false}
                isAnimationActive={false}
              />
              <Line
                type="linear"
                dataKey="committed"
                stroke={CARD_COLOR}
                strokeWidth={2}
                dot={{ r: 3, fill: CARD_COLOR, strokeWidth: 0 }}
                activeDot={{ r: 5, fill: CARD_COLOR, stroke: 'var(--card)', strokeWidth: 2 }}
                isAnimationActive={false}
              />
              {selected && (
                <ReferenceDot
                  x={selected.x}
                  y={selected.committed}
                  r={6}
                  fill={CARD_COLOR}
                  stroke="var(--card)"
                  strokeWidth={2}
                  ifOverflow="extendDomain"
                />
              )}
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  )
}
