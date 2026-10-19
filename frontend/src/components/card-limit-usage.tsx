import { useTranslation } from 'react-i18next'
import { format } from 'date-fns'
import { formatCurrency } from '@/lib/format'
import { resolveDateFnsLocale } from '@/lib/date-fns-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { cn } from '@/lib/utils'
import { limitUsage } from '@/lib/card-forecast'
import type { CardStatement } from '@/types'

interface Props {
  statements: CardStatement[]
  limit: number | null
  availableCredit: number | null
  currency: string
  selectedMonth: string | null
  onSelect: (month: string) => void
}

/** How each statement weighs on the credit limit: unpaid closed bills plus
 *  everything posted and every parcel of the open and future ones. Recurring
 *  charges are left out — they reserve nothing until they are charged. */
export function CardLimitUsage({ statements, limit, availableCredit, currency, selectedMonth, onSelect }: Props) {
  const { t, i18n } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const dfLocale = resolveDateFnsLocale(i18n.resolvedLanguage ?? i18n.language)
  const usage = limitUsage(statements, limit, availableCredit)
  if (limit == null || limit <= 0) return null

  const money = (v: number) => mask(formatCurrency(v, currency, locale))
  const monthLabel = (month: string) => format(new Date(month + 'T00:00:00'), 'MMM yy', { locale: dfLocale })
  const pct = (usage.used / limit) * 100
  const widthOf = (amount: number) => `${Math.max(0.8, (amount / Math.max(limit, usage.used)) * 100)}%`
  const selectedPart = usage.parts.find(p => p.month === selectedMonth)

  return (
    <div data-testid="card-limit-usage">
      <div className="flex items-baseline justify-between mb-2">
        <p className="text-[10px] sm:text-xs font-medium text-muted-foreground uppercase tracking-wide">
          {t('accounts.utilization')}
        </p>
        <p className={cn('text-sm font-bold tabular-nums', pct >= 100 ? 'text-rose-500' : 'text-foreground')}>
          {pct.toFixed(1)}%
        </p>
      </div>
      <div className="flex h-3 w-full gap-0.5 rounded-full bg-muted/60 overflow-hidden mb-2" role="img"
        aria-label={t('accounts.limitUsage.aria', { used: money(usage.used), limit: money(limit) })}>
        {usage.parts.map(part => {
          const isSelected = part.month === selectedMonth
          return (
            <button
              key={part.month}
              type="button"
              onClick={() => onSelect(part.month)}
              title={`${monthLabel(part.month)} · ${money(part.amount)}`}
              aria-label={`${monthLabel(part.month)} · ${money(part.amount)}`}
              data-selected={isSelected || undefined}
              className={cn(
                'h-full shrink-0 transition-colors first:rounded-l-full',
                isSelected ? 'bg-rose-500' : 'bg-rose-300 hover:bg-rose-400 dark:bg-rose-500/40 dark:hover:bg-rose-500/60',
              )}
              style={{ width: widthOf(part.amount) }}
            />
          )
        })}
        {usage.unexplained > 0 && (
          <span
            className="h-full shrink-0 bg-muted-foreground/30"
            title={`${t('accounts.limitUsage.unexplained')} · ${money(usage.unexplained)}`}
            style={{ width: widthOf(usage.unexplained) }}
          />
        )}
      </div>
      <p className="text-xs text-muted-foreground tabular-nums mb-3">
        {money(usage.used)} / {money(limit)}
        {selectedPart && (
          <span className="ml-2">
            · {t('accounts.limitUsage.selected', { month: monthLabel(selectedPart.month), amount: money(selectedPart.amount) })}
          </span>
        )}
      </p>
      <div className="flex flex-wrap gap-1.5 mb-4">
        {usage.parts.map(part => {
          const isSelected = part.month === selectedMonth
          return (
            <button
              key={part.month}
              type="button"
              onClick={() => onSelect(part.month)}
              aria-pressed={isSelected}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] tabular-nums transition-colors',
                isSelected
                  ? 'border-rose-300 bg-rose-50 text-foreground dark:border-rose-500/50 dark:bg-rose-500/10'
                  : 'border-border text-muted-foreground hover:bg-muted/50',
              )}
            >
              <span className={cn('h-2 w-2 rounded-sm', isSelected ? 'bg-rose-500' : 'bg-rose-300 dark:bg-rose-500/40')} />
              <span className="capitalize">{monthLabel(part.month)}</span>
              <span className={cn(isSelected && 'font-semibold')}>{money(part.amount)}</span>
            </button>
          )
        })}
        {usage.unexplained > 0 && (
          <span className="inline-flex items-center gap-1.5 rounded-md border border-dashed border-border px-2 py-1 text-[11px] tabular-nums text-muted-foreground"
            title={t('accounts.limitUsage.unexplainedHint')}>
            <span className="h-2 w-2 rounded-sm bg-muted-foreground/30" />
            {t('accounts.limitUsage.unexplained')}
            <span>{money(usage.unexplained)}</span>
          </span>
        )}
      </div>
    </div>
  )
}
