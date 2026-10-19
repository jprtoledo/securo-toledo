import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Repeat } from 'lucide-react'
import { recurring as recurringApi, transactions as transactionsApi } from '@/lib/api'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { Transaction } from '@/types'

interface TransactionRecurrencePanelProps {
  transaction: Transaction
  canWrite?: boolean
  onChanged?: (updated: Transaction) => void
}

/** The recurrence a row belongs to and the user's decisions about it
 *  (RFC toledo-features §8.6): confirm a changed amount, accept a conflict
 *  without moving the expected amount, reject a link, link by hand, mark
 *  as "not recurring" or start a recurrence from the row. */
export function TransactionRecurrencePanel({ transaction, canWrite = true, onChanged }: TransactionRecurrencePanelProps) {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [current, setCurrent] = useState(transaction)
  const [busy, setBusy] = useState(false)

  const { data: recurrences } = useQuery({
    queryKey: ['recurring'],
    queryFn: recurringApi.list,
  })
  const linkedId = current.recurring_transaction_id ?? current.recurring_manual_id ?? null
  const linked = recurrences?.find(r => r.id === linkedId)
  const candidates = (recurrences ?? []).filter(r =>
    r.id !== linkedId && r.is_active
      && (!r.account_id || r.account_id === current.account_id || (!!current.account_id && !!r.extra_account_ids?.includes(current.account_id))))
  const isParcel = !!current.installment_number && (current.total_installments ?? 0) > 1

  const run = async (action: () => Promise<Transaction>) => {
    if (busy) return
    setBusy(true)
    try {
      const updated = await action()
      setCurrent(updated)
      invalidateFinancialQueries(queryClient)
      queryClient.invalidateQueries({ queryKey: ['recurring'] })
      onChanged?.(updated)
      toast.success(t('recurring.decisionSaved'))
    } catch {
      toast.error(t('common.error'))
    } finally {
      setBusy(false)
    }
  }

  const periodLabel = (value: string) =>
    new Date(value + 'T00:00:00').toLocaleDateString(i18n.language, { month: 'short', year: 'numeric' })

  const btn = 'h-6 text-xs'
  if (!linkedId && (isParcel || current.card_line_kind === 'payment' || current.card_line_kind === 'previous_balance')) {
    return null
  }

  return (
    <div className="space-y-1.5 rounded-md border border-border/60 px-2.5 py-2 text-xs" data-testid="transaction-recurrence-panel">
      {linkedId ? (
        <p className="flex flex-wrap items-center gap-2">
          <Repeat size={13} className="shrink-0 text-muted-foreground" />
          <span className="font-medium">{linked?.description ?? t('recurring.linkedUnknown')}</span>
          {current.recurring_period && <span className="text-muted-foreground">{periodLabel(current.recurring_period)}</span>}
          <span
            className={cn(
              'rounded-full px-1.5 py-0.5 text-[10px] font-semibold',
              current.recurring_link === 'conflict'
                ? 'bg-rose-500/10 text-rose-600'
                : 'bg-muted text-muted-foreground',
            )}
          >
            {t(`recurring.link.${current.recurring_link ?? 'auto'}`)}
          </span>
          {current.recurring_companion && <span className="text-muted-foreground">{t('recurring.companion')}</span>}
        </p>
      ) : (
        <p className="flex items-center gap-2 text-muted-foreground">
          <Repeat size={13} className="shrink-0" />
          {current.recurring_manual_none ? t('recurring.markedNotRecurring') : t('recurring.notLinked')}
        </p>
      )}
      {current.recurring_issue && (
        <p className="text-rose-600">
          {t(`recurring.issue.${current.recurring_issue.code}`, {
            charged: current.recurring_issue.charged,
            expected: current.recurring_issue.expected,
          })}
        </p>
      )}
      {canWrite && (
        <div className="flex flex-wrap items-center gap-2">
          {linkedId && current.recurring_link === 'conflict' && (
            <>
              <Button type="button" size="sm" variant="outline" className={btn} disabled={busy}
                title={t('recurring.confirmHint')}
                onClick={() => run(() => transactionsApi.confirmRecurring(current.id))}>
                {t('recurring.confirmAmount')}
              </Button>
              <Button type="button" size="sm" variant="outline" className={btn} disabled={busy}
                title={t('recurring.acceptHint')}
                onClick={() => run(() => transactionsApi.reviewRecurring(current.id, 'accept'))}>
                {t('recurring.accept')}
              </Button>
            </>
          )}
          {linkedId && current.recurring_link !== 'manual' && (
            <Button type="button" size="sm" variant="outline" className={btn} disabled={busy}
              onClick={() => run(() => transactionsApi.reviewRecurring(current.id, 'reject'))}>
              {t('recurring.reject')}
            </Button>
          )}
          {(current.recurring_link === 'manual' || current.recurring_manual_none) && (
            <Button type="button" size="sm" variant="ghost" className={btn} disabled={busy}
              onClick={() => run(() => transactionsApi.linkRecurring(current.id, null))}>
              {t('recurring.undoDecision')}
            </Button>
          )}
          {!linkedId && !current.recurring_manual_none && (
            <>
              <Button type="button" size="sm" variant="outline" className={btn} disabled={busy}
                onClick={() => navigate(`/recurring?fromTransaction=${current.id}`)}>
                {t('recurring.makeRecurring')}
              </Button>
              <Button type="button" size="sm" variant="ghost" className={btn} disabled={busy}
                onClick={() => run(() => transactionsApi.linkRecurring(current.id, 'none'))}>
                {t('recurring.notRecurring')}
              </Button>
            </>
          )}
          {candidates.length > 0 && (
            <select
              className="h-6 rounded-md border border-border bg-card px-1 text-xs"
              value=""
              aria-label={t('recurring.linkTo')}
              onChange={(e) => e.target.value && run(() => transactionsApi.linkRecurring(current.id, e.target.value))}
            >
              <option value="">{t('recurring.linkTo')}</option>
              {candidates.map(r => <option key={r.id} value={r.id}>{r.description}</option>)}
            </select>
          )}
        </div>
      )}
    </div>
  )
}
