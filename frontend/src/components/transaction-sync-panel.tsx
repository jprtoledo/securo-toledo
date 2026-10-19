import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ChevronDown, ChevronRight, Copy, Lock, Trash2 } from 'lucide-react'
import { accounts as accountsApi, transactions as transactionsApi } from '@/lib/api'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { Transaction } from '@/types'

interface TransactionSyncPanelProps {
  transaction: Transaction
  onChanged?: (updated: Transaction) => void
}

/** Sync state of a bank-synced row (RFC toledo-features §4.9): removed by
 *  the bank, duplicate of another row, the "Don't update from the bank"
 *  lock, and the read-only provider details. */
export function TransactionSyncPanel({ transaction, onChanged }: TransactionSyncPanelProps) {
  const { t, i18n } = useTranslation()
  const queryClient = useQueryClient()
  const [current, setCurrent] = useState(transaction)
  const [busy, setBusy] = useState(false)
  const [showDetails, setShowDetails] = useState(false)

  const { data: original } = useQuery({
    queryKey: ['transactions', current.duplicate_of_id],
    queryFn: () => transactionsApi.get(current.duplicate_of_id!),
    enabled: !!current.duplicate_of_id,
  })

  const isParcel = !!current.installment_number && (current.total_installments ?? 0) > 1
  const { data: plansData } = useQuery({
    queryKey: ['accounts', current.account_id, 'installment-plans'],
    queryFn: () => accountsApi.installmentPlans(current.account_id!),
    enabled: isParcel && !!current.account_id,
  })
  const currentPlan = plansData?.plans.find(p => p.id === current.installment_plan_id)
  const otherPlans = (plansData?.plans ?? []).filter(p =>
    p.id !== current.installment_plan_id
    && p.total_installments === current.total_installments
    && p.source !== 'series'
    && !p.installments.some(i => i.kind === 'posted' && i.number === current.installment_number))

  const apply = (updated: Transaction) => {
    setCurrent(updated)
    invalidateFinancialQueries(queryClient)
    onChanged?.(updated)
  }

  const run = async (action: () => Promise<Transaction>, success: string) => {
    if (busy) return
    setBusy(true)
    try {
      apply(await action())
      toast.success(success)
    } catch {
      toast.error(t('common.error'))
    } finally {
      setBusy(false)
    }
  }

  const formatDate = (value: string) =>
    new Date(value).toLocaleDateString(i18n.language, { day: '2-digit', month: 'short', year: 'numeric' })

  const details: [string, string | null | undefined][] = [
    [t('transactions.sync.operationType'), current.operation_type],
    [t('transactions.sync.cardNumber'), current.card_number ? `•••• ${current.card_number}` : null],
    [t('transactions.sync.merchant'), current.merchant_name],
    [t('transactions.sync.mcc'), current.merchant_mcc],
    [t('transactions.sync.providerCategory'), current.provider_category],
    [t('transactions.sync.paymentType'), current.payment_type],
    [t('transactions.sync.billId'), current.provider_bill_external_id],
    [t('transactions.sync.rawDescription'), current.provider_description_raw],
  ]
  const visibleDetails = details.filter(([, value]) => !!value)

  return (
    <div className="space-y-2 rounded-md border border-border/60 p-3" data-testid="transaction-sync-panel">
      {current.provider_removed_at && (
        <div className="flex items-start gap-2 rounded-md bg-amber-500/10 px-2.5 py-2 text-xs text-amber-700 dark:text-amber-400">
          <Trash2 size={14} className="mt-0.5 shrink-0" />
          <div>
            <p className="font-medium">
              {t('transactions.sync.removedByBank', { date: formatDate(current.provider_removed_at) })}
            </p>
            <p className="opacity-80">
              {current.is_ignored ? t('transactions.sync.removedHint') : t('transactions.sync.removedKept')}
            </p>
          </div>
        </div>
      )}

      {current.duplicate_of_id && (
        <div className="flex flex-wrap items-center gap-2 rounded-md bg-muted/60 px-2.5 py-2 text-xs">
          <Copy size={14} className="shrink-0 text-muted-foreground" />
          <span className="min-w-0 flex-1">
            {original
              ? t('transactions.sync.duplicateOf', {
                  description: original.description,
                  date: formatDate(original.date),
                })
              : t('transactions.sync.duplicateOfUnknown')}
          </span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-7 text-xs"
            disabled={busy}
            onClick={() => run(() => transactionsApi.markNotDuplicate(current.id), t('transactions.sync.notDuplicateDone'))}
          >
            {t('transactions.sync.notDuplicate')}
          </Button>
        </div>
      )}

      {current.effective_date && current.effective_date_source && current.effective_date_source !== 'tx_date' && (
        <p className="text-xs text-muted-foreground" data-testid="why-this-month">
          {t('transactions.sync.placedIn', { date: formatDate(current.effective_date) })}{' '}
          {t(`transactions.sync.placedBy.${current.effective_date_source}`)}
        </p>
      )}
      {current.card_line_kind && (
        <p className="text-xs text-muted-foreground">
          {t('transactions.sync.cardKindIs', { kind: t(`rules.cardKind.${current.card_line_kind}`) })}
          {current.card_line_kind_reason === 'carried_over' && ` ${t('transactions.sync.carriedOver')}`}
          {current.card_line_kind_source === 'rule' && ` ${t('transactions.sync.byRule')}`}
        </p>
      )}

      {isParcel && (
        <div className="space-y-1.5 rounded-md bg-muted/40 px-2.5 py-2 text-xs" data-testid="installment-plan-info">
          <p>
            <span className="font-medium">
              {current.installment_number}/{current.total_installments}
              {currentPlan ? ` · ${currentPlan.description}` : ''}
            </span>
            {current.installment_link && (
              <span className={cn('ml-2', current.installment_link === 'conflict' ? 'text-rose-600' : 'text-muted-foreground')}>
                {t(`installments.link.${current.installment_link}`)}
              </span>
            )}
          </p>
          {current.installment_issue && (
            <p className="text-rose-600">
              {t(`installments.issue.${current.installment_issue.code}`, { expected: current.installment_issue.expected })}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            {current.installment_link === 'conflict' && (
              <Button type="button" size="sm" variant="outline" className="h-6 text-xs" disabled={busy}
                onClick={() => run(() => transactionsApi.confirmInstallmentPlan(current.id), t('installments.saved'))}>
                {t('installments.confirm')}
              </Button>
            )}
            <Button type="button" size="sm" variant="outline" className="h-6 text-xs" disabled={busy}
              onClick={() => run(() => transactionsApi.linkInstallmentPlan(current.id, 'new'), t('installments.saved'))}>
              {t('installments.split')}
            </Button>
            {current.installment_link === 'manual' && (
              <Button type="button" size="sm" variant="ghost" className="h-6 text-xs" disabled={busy}
                onClick={() => run(() => transactionsApi.linkInstallmentPlan(current.id, null), t('installments.saved'))}>
                {t('installments.undo')}
              </Button>
            )}
            {otherPlans.length > 0 && (
              <select
                className="h-6 rounded-md border border-border bg-card px-1 text-xs"
                value=""
                aria-label={t('installments.moveTo')}
                onChange={(e) => e.target.value && run(
                  () => transactionsApi.linkInstallmentPlan(current.id, e.target.value), t('installments.saved'),
                )}
              >
                <option value="">{t('installments.moveTo')}</option>
                {otherPlans.map(p => <option key={p.id} value={p.id}>{p.description}</option>)}
              </select>
            )}
          </div>
        </div>
      )}

      <label className="flex items-start justify-between gap-3 text-sm cursor-pointer">
        <span className="flex items-start gap-2">
          <Lock size={14} className="mt-0.5 text-muted-foreground" />
          <span>
            <span className="font-medium">{t('transactions.sync.lock')}</span>
            <span className="block text-xs text-muted-foreground">{t('transactions.sync.lockHint')}</span>
          </span>
        </span>
        <input
          type="checkbox"
          checked={!!current.sync_locked}
          disabled={busy}
          onChange={(e) =>
            run(
              () => transactionsApi.setSyncLock(current.id, e.target.checked),
              e.target.checked ? t('transactions.sync.locked') : t('transactions.sync.unlocked'),
            )
          }
          className="mt-1 h-4 w-4 rounded border-border text-primary focus:ring-primary"
          aria-label={t('transactions.sync.lock')}
        />
      </label>

      {visibleDetails.length > 0 && (
        <div>
          <button
            type="button"
            onClick={() => setShowDetails((v) => !v)}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            {showDetails ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            {t('transactions.sync.providerDetails')}
          </button>
          {showDetails && (
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
              {visibleDetails.map(([label, value]) => (
                <div key={label} className="contents">
                  <dt className="text-muted-foreground">{label}</dt>
                  <dd className="min-w-0 break-words font-mono">{value}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      )}
    </div>
  )
}
