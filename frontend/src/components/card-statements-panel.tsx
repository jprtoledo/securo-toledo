import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { Info } from 'lucide-react'
import { accounts as accountsApi } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import { useDateLocale, useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { cn } from '@/lib/utils'
import type { CardStatement, CardStatementsResponse } from '@/types'

const STATE_STYLE: Record<CardStatement['state'], string> = {
  closed: 'bg-muted text-muted-foreground',
  closed_estimated: 'bg-amber-500/10 text-amber-700 dark:text-amber-400',
  open: 'bg-sky-500/10 text-sky-700 dark:text-sky-300',
  future: 'bg-violet-500/10 text-violet-700 dark:text-violet-300',
}

const STATUS_STYLE: Record<CardStatement['status'], string> = {
  paid: 'text-emerald-600',
  due: 'text-amber-600',
  overdue: 'text-rose-600',
  nothing_due: 'text-muted-foreground',
  not_closed: 'text-muted-foreground',
  unknown: 'text-muted-foreground',
}

function monthLabel(month: string, locale: string) {
  return new Date(month + 'T00:00:00').toLocaleDateString(locale, { month: 'short', year: 'numeric' })
}

function dayLabel(d: string | null, locale: string) {
  return d ? new Date(d + 'T00:00:00').toLocaleDateString(locale, { day: '2-digit', month: 'short' }) : '—'
}

/** How open-bill purchases are placed (RFC §6.2): the calibrated order of
 *  the fallbacks and their measured accuracy. */
function PlacementInfo({ data }: { data: CardStatementsResponse }) {
  const { t } = useTranslation()
  const stats = data.resolution_stats
  return (
    <details className="text-xs text-muted-foreground">
      <summary className="inline-flex cursor-pointer items-center gap-1 hover:text-foreground">
        <Info size={12} /> {t('accounts.statements.howPlaced')}
      </summary>
      <div className="mt-2 space-y-1 rounded-md border border-border/60 p-2">
        <p>
          {data.closing_day && data.due_day
            ? t(data.cycle_days_manual ? 'accounts.statements.cycleManual' : 'accounts.statements.cycleDerived', {
                closing: data.closing_day, due: data.due_day,
              })
            : t('accounts.statements.cycleUnknown')}
        </p>
        {stats && (
          <>
            <p>{t('accounts.statements.truthSamples', { count: stats.truth_samples })}</p>
            {stats.forecast_offset_months !== 0 && (
              <p>{t('accounts.statements.forecastOffset', { count: stats.forecast_offset_months })}</p>
            )}
            <ol className="list-decimal pl-4">
              {stats.fallback_order.map(method => {
                const m = stats.methods[method]
                return (
                  <li key={method}>
                    {t(`accounts.statements.method.${method}`)}
                    {m && m.samples > 0 && ` — ${m.hits}/${m.samples}`}
                  </li>
                )
              })}
            </ol>
          </>
        )}
      </div>
    </details>
  )
}

/** Credit-card statements, one per bill month (RFC toledo-features §6.4). */
export function CardStatementsPanel({ accountId, currency }: { accountId: string; currency: string }) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const { mask } = usePrivacyMode()
  const { data, isLoading, isError } = useQuery({
    queryKey: ['accounts', accountId, 'statements'],
    queryFn: () => accountsApi.statements(accountId),
  })
  const money = (n: number) => mask(formatCurrency(n, currency, locale))

  if (isLoading) return <p className="text-sm text-muted-foreground p-4">{t('common.loading')}</p>
  if (isError || !data) return null
  if (!data.statements.length) {
    return <p className="text-sm text-muted-foreground p-4">{t('accounts.statements.empty')}</p>
  }
  const statements = [...data.statements].reverse()

  return (
    <div className="space-y-3" data-testid="card-statements">
      <PlacementInfo data={data} />
      <div className="flex gap-3 overflow-x-auto pb-2">
        {statements.map(s => (
          <div
            key={s.month}
            className={cn(
              'min-w-[220px] shrink-0 rounded-xl border border-border/60 bg-card p-3 space-y-1.5',
              s.month === data.open_month && 'ring-2 ring-primary/40',
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-semibold capitalize">{monthLabel(s.month, dateLocale)}</span>
              <span className={cn('rounded-full px-1.5 text-[10px] font-semibold', STATE_STYLE[s.state])}>
                {t(`accounts.statements.state.${s.state}`)}
              </span>
            </div>
            <p className="text-lg font-semibold tabular-nums">
              {s.total_estimated && <span className="text-muted-foreground">≈ </span>}
              {money(s.total)}
            </p>
            <p className={cn('text-xs font-medium', STATUS_STYLE[s.status])}>
              {t(`accounts.statements.status.${s.status}`, {
                date: dayLabel(s.paid_at, dateLocale),
              })}
            </p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-2 text-xs text-muted-foreground">
              <dt>{t('accounts.statements.closing')}</dt>
              <dd className="text-right">{dayLabel(s.closing_date, dateLocale)}{s.dates_estimated && '*'}</dd>
              <dt>{t('accounts.statements.due')}</dt>
              <dd className="text-right">{dayLabel(s.due_date, dateLocale)}{s.dates_estimated && '*'}</dd>
              <dt>{t('accounts.statements.spent')}</dt>
              <dd className="text-right tabular-nums">{money(s.spent)}</dd>
              {s.prepaid > 0 && (
                <>
                  <dt>{t('accounts.statements.prepaid')}</dt>
                  <dd className="text-right tabular-nums">−{money(s.prepaid)}</dd>
                </>
              )}
              {s.paid > 0 && (
                <>
                  <dt>{t('accounts.statements.paid')}</dt>
                  <dd className="text-right tabular-nums">{money(s.paid)}</dd>
                </>
              )}
              {s.projected_installment_count > 0 && (
                <>
                  <dt>{t('accounts.statements.projectedInstallments', { count: s.projected_installment_count })}</dt>
                  <dd className="text-right tabular-nums">{money(s.projected_installments)}</dd>
                </>
              )}
              {s.projected_recurring_count > 0 && (
                <>
                  <dt>{t('accounts.statements.projectedRecurring', { count: s.projected_recurring_count })}</dt>
                  <dd className="text-right tabular-nums">{money(s.projected_recurring)}</dd>
                </>
              )}
              {s.minimum_payment != null && (
                <>
                  <dt>{t('accounts.statements.minimum')}</dt>
                  <dd className="text-right tabular-nums">{money(s.minimum_payment)}</dd>
                </>
              )}
            </dl>
            {s.reconciliation && Math.abs(s.reconciliation.diff) >= 0.01 && (
              <p className="text-[11px] text-amber-700 dark:text-amber-400">
                {t('accounts.statements.reconciliationDiff', { diff: money(s.reconciliation.diff) })}
              </p>
            )}
            {s.history === 'before' && (
              <p className="text-[11px] text-muted-foreground">{t('accounts.statements.beforeHistory')}</p>
            )}
            {s.history === 'first' && (
              <p className="text-[11px] text-muted-foreground">{t('accounts.statements.firstHistory')}</p>
            )}
          </div>
        ))}
      </div>
      <p className="text-[11px] text-muted-foreground">{t('accounts.statements.estimatedNote')}</p>
    </div>
  )
}
