import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { AlertTriangle, Check, GitMerge, Scissors, Undo2 } from 'lucide-react'
import { accounts as accountsApi, transactions as transactionsApi } from '@/lib/api'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import { formatCurrency } from '@/lib/format'
import { useDateLocale, useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { cn } from '@/lib/utils'
import type { InstallmentPlanParcel, InstallmentPlanView } from '@/types'

const PARCEL_STYLE: Record<InstallmentPlanParcel['kind'], string> = {
  posted: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30',
  projected: 'bg-sky-500/10 text-sky-700 dark:text-sky-300 border-dashed border-sky-500/40',
  missing: 'bg-rose-500/10 text-rose-700 dark:text-rose-300 border-rose-500/30',
  before_history: 'bg-muted text-muted-foreground border-border',
}

type ApiError = { response?: { data?: { detail?: string } } }

/** The card's installment plans (RFC toledo-features §7.4): each purchase
 *  with its parcel timeline and the decisions the user can make. */
export function InstallmentPlansPanel({ accountId, currency }: { accountId: string; currency: string }) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const { mask } = usePrivacyMode()
  const queryClient = useQueryClient()
  const [mergeFrom, setMergeFrom] = useState<string | null>(null)
  const { data, isLoading } = useQuery({
    queryKey: ['accounts', accountId, 'installment-plans'],
    queryFn: () => accountsApi.installmentPlans(accountId),
  })
  const money = (n: number) => mask(formatCurrency(n, currency, locale))
  const month = (m: string) => new Date(m + 'T00:00:00').toLocaleDateString(dateLocale, { month: 'short', year: '2-digit' })

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['accounts', accountId] })
    invalidateFinancialQueries(queryClient)
  }
  const decide = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => { refresh(); toast.success(t('installments.saved')) },
    onError: (err: ApiError) => toast.error(err.response?.data?.detail || t('common.error')),
  })

  if (isLoading) return <p className="p-4 text-sm text-muted-foreground">{t('common.loading')}</p>
  if (!data?.plans.length) return <p className="p-4 text-sm text-muted-foreground">{t('installments.empty')}</p>

  const mergeSource = data.plans.find(p => p.id === mergeFrom)
  const mergeTargets = (plan: InstallmentPlanView) => data.plans.filter(p =>
    p.id !== plan.id
    && p.total_installments === plan.total_installments
    && !p.installments.some(i => i.kind === 'posted'
      && plan.installments.some(j => j.kind === 'posted' && j.number === i.number)))

  return (
    <div className="space-y-3" data-testid="installment-plans">
      {(data.alerts.conflicts > 0 || data.alerts.missing > 0) && (
        <p className="flex items-center gap-1.5 text-xs text-amber-700 dark:text-amber-400">
          <AlertTriangle size={13} />
          {t('installments.alerts', { conflicts: data.alerts.conflicts, missing: data.alerts.missing })}
        </p>
      )}
      {data.plans.map(plan => (
        <div key={plan.id} className={cn('rounded-lg border border-border/60 p-3 space-y-2', !plan.active && 'opacity-70')}>
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">{plan.description || t('installments.unnamed')}</p>
              <p className="text-xs text-muted-foreground">
                {t('installments.summary', {
                  current: plan.current_installment, total: plan.total_installments,
                  amount: money(plan.installment_amount), total_amount: money(plan.total_amount),
                })}
                {plan.remaining > 0 && ` · ${t('installments.remaining', { amount: money(plan.remaining) })}`}
                {plan.source !== 'detected' && ` · ${t(`installments.source.${plan.source}`)}`}
              </p>
            </div>
            {plan.source !== 'series' && (
              <button
                type="button"
                className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                onClick={() => setMergeFrom(mergeFrom === plan.id ? null : plan.id)}
              >
                <GitMerge size={12} /> {t('installments.merge')}
              </button>
            )}
          </div>
          {mergeSource?.id === plan.id && (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="text-muted-foreground">{t('installments.mergeInto')}</span>
              {mergeTargets(plan).length === 0 && <span>{t('installments.noMergeTarget')}</span>}
              {mergeTargets(plan).map(target => (
                <button
                  key={target.id}
                  type="button"
                  className="rounded-md border border-border px-2 py-0.5 hover:bg-muted"
                  onClick={() => {
                    setMergeFrom(null)
                    decide.mutate(() => transactionsApi.mergeInstallmentPlans(plan.id, target.id))
                  }}
                >
                  {target.description} ({month(target.first_month)})
                </button>
              ))}
            </div>
          )}
          <div className="flex flex-wrap gap-1">
            {plan.installments.map(parcel => (
              <ParcelChip
                key={`${parcel.number}-${parcel.kind}-${parcel.transaction_id ?? ''}`}
                parcel={parcel}
                label={month(parcel.month)}
                amount={money(parcel.amount)}
                onConfirm={parcel.transaction_id && parcel.link === 'conflict'
                  ? () => decide.mutate(() => transactionsApi.confirmInstallmentPlan(parcel.transaction_id!))
                  : undefined}
                onSplit={parcel.transaction_id && plan.source !== 'series'
                  ? () => decide.mutate(() => transactionsApi.linkInstallmentPlan(parcel.transaction_id!, 'new'))
                  : undefined}
                onUndo={parcel.transaction_id && parcel.link === 'manual'
                  ? () => decide.mutate(() => transactionsApi.linkInstallmentPlan(parcel.transaction_id!, null))
                  : undefined}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function ParcelChip({
  parcel, label, amount, onConfirm, onSplit, onUndo,
}: {
  parcel: InstallmentPlanParcel
  label: string
  amount: string
  onConfirm?: () => void
  onSplit?: () => void
  onUndo?: () => void
}) {
  const { t } = useTranslation()
  const issue = parcel.issue
    ? t(`installments.issue.${parcel.issue.code}`, { expected: parcel.issue.expected })
    : null
  const title = [
    `${parcel.number} · ${label} · ${amount}`,
    t(`installments.kind.${parcel.kind}`),
    parcel.pending ? t('installments.pending') : null,
    issue,
  ].filter(Boolean).join(' — ')
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] tabular-nums',
        PARCEL_STYLE[parcel.kind],
        parcel.link === 'conflict' && 'ring-1 ring-rose-500',
      )}
      title={title}
    >
      <span className="font-semibold">{parcel.number}</span>
      <span>{label}</span>
      {parcel.link === 'manual' && <span aria-label={t('installments.manualLink')}>•</span>}
      {onConfirm && (
        <button type="button" onClick={onConfirm} title={t('installments.confirm')} aria-label={t('installments.confirm')}>
          <Check size={11} />
        </button>
      )}
      {onSplit && (
        <button type="button" onClick={onSplit} title={t('installments.split')} aria-label={t('installments.split')}>
          <Scissors size={11} />
        </button>
      )}
      {onUndo && (
        <button type="button" onClick={onUndo} title={t('installments.undo')} aria-label={t('installments.undo')}>
          <Undo2 size={11} />
        </button>
      )}
    </span>
  )
}
