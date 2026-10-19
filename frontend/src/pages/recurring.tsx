import React, { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getAccountName, sortAccountsByDisplayName } from '@/lib/account-utils'
import { useTranslation } from 'react-i18next'
import { useDisplayLocale, useDateLocale } from '@/hooks/use-display-locale'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { categories as categoriesApi, categoryGroups as categoryGroupsApi, recurring as recurringApi, accounts as accountsApi, currencies as currenciesApi, transactions as transactionsApi } from '@/lib/api'
import { billedUnits } from '@/lib/recurrence-utils'
import { ConditionTreeEditor } from '@/components/rule-dialog'
import { extractApiError } from '@/lib/api-errors'
import { localDateString } from '@/lib/date-utils'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { DeleteConfirmationDialog } from '@/components/delete-confirmation-dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import type { Account, Category, CategoryGroup, RecurrenceSlot, RecurringSuggestion, RecurringTransaction, RuleConditionNode } from '@/types'
import { Pencil, Trash2, Plus, RefreshCw, Info, AlertTriangle, X, Sparkles } from 'lucide-react'
import { cn } from '@/lib/utils'
import { PageHeader } from '@/components/page-header'
import { CategorySelect } from '@/components/category-select'
import { DatePickerInput } from '@/components/ui/date-picker-input'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { useAuth } from '@/contexts/auth-context'
import { useWorkspace } from '@/contexts/workspace-context'
import { formatCurrency } from '@/lib/format'

const TH = 'text-xs font-medium text-muted-foreground py-3'

function SectionCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-card rounded-xl border border-border shadow-sm overflow-hidden">
      {children}
    </div>
  )
}

function SectionHeader({ title, action }: { title: string; action?: React.ReactNode }) {
  return (
    <div className="px-4 sm:px-5 py-4 border-b border-border flex flex-wrap items-center justify-between gap-2">
      <p className="text-sm font-semibold text-foreground">{title}</p>
      {action}
    </div>
  )
}

const SLOT_CLASS: Record<RecurrenceSlot['status'], string> = {
  posted: 'bg-emerald-500 border-emerald-500',
  conflict: 'bg-rose-500 border-rose-500',
  expected: 'bg-transparent border-muted-foreground/50',
  late: 'bg-amber-400 border-amber-400',
  missed: 'bg-transparent border-rose-500',
  skipped: 'bg-muted-foreground/25 border-muted-foreground/25',
}

// Occurrences without a charge can be marked as not happening (or undone):
// reports count every occurrence still to come except skipped ones.
const SKIPPABLE = new Set<RecurrenceSlot['status']>(['expected', 'late', 'missed', 'skipped'])

/** The periods around today (RFC toledo-features §8.4): a few settled ones,
 * then the next ones to come. */
function visibleSlots(slots: RecurrenceSlot[]): RecurrenceSlot[] {
  const today = localDateString()
  const next = slots.findIndex((s) => s.status === 'expected' || s.status === 'late' || (s.status === 'skipped' && s.expected_date >= today))
  return next < 0 ? slots.slice(-8) : slots.slice(Math.max(0, next - 4), next + 6)
}

export function RecurrenceTimeline({ slots, currency, onSkip, accountId, accountNames }: {
  slots: RecurrenceSlot[]
  currency: string
  onSkip?: (period: string, skipped: boolean) => void
  // The recurrence's own account and every account's name: a slot paid by
  // another account names it.
  accountId?: string | null
  accountNames?: Record<string, string>
}) {
  const { t, i18n } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  return (
    <div className="mt-1 flex flex-wrap items-center gap-0.5" data-testid="recurrence-timeline">
      {visibleSlots(slots).map((slot) => {
        const label = new Date(slot.expected_date + 'T00:00:00').toLocaleDateString(i18n.language, { month: 'short', year: '2-digit' })
        const amount = slot.amount ?? slot.expected_amount
        const paidElsewhere = slot.account_id && slot.account_id !== accountId ? accountNames?.[slot.account_id] : undefined
        const title = [
          label,
          t(`recurring.slot.${slot.status}`),
          mask(formatCurrency(Number(amount), currency, locale)),
          ...(paidElsewhere ? [t('recurring.paidFrom', { account: paidElsewhere })] : []),
        ].join(' · ')
        const dot = (
          <span className={cn('inline-block h-2.5 w-2.5 rounded-full border-2', SLOT_CLASS[slot.status], slot.excluded && 'opacity-50')} />
        )
        if (!onSkip || !SKIPPABLE.has(slot.status)) {
          return (
            <span key={slot.period} title={title} aria-label={title} data-status={slot.status} className="inline-flex p-1">
              {dot}
            </span>
          )
        }
        const skipped = slot.status === 'skipped'
        return (
          <Popover key={slot.period}>
            <PopoverTrigger asChild>
              <button
                type="button"
                title={title}
                aria-label={title}
                data-status={slot.status}
                className="inline-flex rounded-full p-1 hover:bg-muted-foreground/15 focus-visible:outline-2 focus-visible:outline-ring"
              >
                {dot}
              </button>
            </PopoverTrigger>
            <PopoverContent align="start" className="w-64 p-3">
              <p className="text-xs font-medium text-foreground">{label} · {mask(formatCurrency(Number(amount), currency, locale))}</p>
              <p className="mt-0.5 text-xs text-muted-foreground">{t(`recurring.slot.${slot.status}`)}</p>
              <p className="mt-2 text-xs text-muted-foreground">{t(skipped ? 'recurring.skip.undoHint' : 'recurring.skip.hint')}</p>
              <Button
                size="sm"
                variant="outline"
                className="mt-3 w-full"
                onClick={() => onSkip(slot.period, !skipped)}
              >
                {t(skipped ? 'recurring.skip.undo' : 'recurring.skip.action')}
              </Button>
            </PopoverContent>
          </Popover>
        )
      })}
    </div>
  )
}

/** Empty condition tree ↔ no condition (the legacy matcher). */
function toConditionTree(node: RuleConditionNode | null | undefined): { op: 'and' | 'or'; conditions: RuleConditionNode[] } {
  if (!node) return { op: 'and', conditions: [] }
  if ('conditions' in node && Array.isArray(node.conditions)) return { op: node.op, conditions: node.conditions }
  return { op: 'and', conditions: [node] }
}

export default function RecurringPage() {
  const { t } = useTranslation()

  return (
    <div>
      <PageHeader section={t('recurring.title')} title={t('recurring.title')} />
      <RecurringTab />
    </div>
  )
}

function RecurringTab() {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const { mask } = usePrivacyMode()
  const { user } = useAuth()
  const { canWrite } = useWorkspace()
  const userCurrency = user?.preferences?.currency_display ?? 'USD'
  const queryClient = useQueryClient()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<RecurringTransaction | null>(null)
  const [deletingRecurring, setDeletingRecurring] = useState<RecurringTransaction | null>(null)
  const [draft, setDraft] = useState<Partial<RecurringTransaction> | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const fromTransaction = searchParams.get('fromTransaction')

  // "Make recurring" from a transaction (RFC §8.6): open the form prefilled.
  useEffect(() => {
    if (!fromTransaction) return
    let cancelled = false
    transactionsApi.recurringDraft(fromTransaction)
      .then((data) => {
        if (cancelled) return
        setEditing(null)
        setDraft(data)
        setDialogOpen(true)
      })
      .catch((err: unknown) => toast.error(extractApiError(err, t('common.error'))))
      .finally(() => {
        if (!cancelled) setSearchParams((prev) => { prev.delete('fromTransaction'); return prev }, { replace: true })
      })
    return () => { cancelled = true }
  }, [fromTransaction, setSearchParams, t])

  const closeDialog = () => { setDialogOpen(false); setEditing(null); setDraft(null) }
  const savedToast = (message: string, data: RecurringTransaction) => {
    const linked = data.link_stats?.linked
    toast.success(linked ? `${message} · ${t('recurring.linkedCount', { count: linked })}` : message)
  }

  const { data: recurringList } = useQuery({
    queryKey: ['recurring'],
    queryFn: recurringApi.list,
  })

  const { data: categoriesList } = useQuery({
    queryKey: ['categories'],
    queryFn: categoriesApi.list,
  })

  const { data: allCategoriesList } = useQuery({
    queryKey: ['categories', 'management'],
    queryFn: categoriesApi.listIncludingHidden,
    enabled: Boolean(editing?.category_id),
  })

  const { data: categoryGroupsList } = useQuery({
    queryKey: ['categoryGroups'],
    queryFn: categoryGroupsApi.list,
  })

  const { data: accountsList } = useQuery({
    queryKey: ['accounts'],
    queryFn: () => accountsApi.list(),
  })
  const accountNames = useMemo(
    () => Object.fromEntries((accountsList ?? []).map((a) => [a.id, getAccountName(a)])) as Record<string, string>,
    [accountsList],
  )

  const createMutation = useMutation({
    mutationFn: (data: Partial<RecurringTransaction>) => recurringApi.create(data),
    onSuccess: (data) => {
      invalidateFinancialQueries(queryClient)
      queryClient.invalidateQueries({ queryKey: ['recurring'] })
      queryClient.invalidateQueries({ queryKey: ['recurring-suggestions'] })
      closeDialog()
      savedToast(t('recurring.created'), data)
    },
    onError: (err: unknown) => toast.error(extractApiError(err, t('common.error'))),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, ...data }: Partial<RecurringTransaction> & { id: string }) =>
      recurringApi.update(id, data),
    onSuccess: (data) => {
      invalidateFinancialQueries(queryClient)
      queryClient.invalidateQueries({ queryKey: ['recurring'] })
      closeDialog()
      savedToast(t('recurring.updated'), data)
    },
    onError: (err: unknown) => toast.error(extractApiError(err, t('common.error'))),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => recurringApi.delete(id),
    onSuccess: () => {
      invalidateFinancialQueries(queryClient)
      queryClient.invalidateQueries({ queryKey: ['recurring'] })
      setDeletingRecurring(null)
      toast.success(t('recurring.deleted'))
    },
    onError: (err: unknown) => {
      toast.error(extractApiError(err, t('common.error')))
    },
  })

  const endMutation = useMutation({
    mutationFn: (id: string) => recurringApi.end(id),
    onSuccess: () => {
      invalidateFinancialQueries(queryClient)
      queryClient.invalidateQueries({ queryKey: ['recurring'] })
      toast.success(t('recurring.ended'))
    },
    onError: (err: unknown) => toast.error(extractApiError(err, t('common.error'))),
  })

  const skipMutation = useMutation({
    mutationFn: ({ id, period, skipped }: { id: string; period: string; skipped: boolean }) =>
      recurringApi.skip(id, period, skipped),
    onSuccess: (_data, { skipped }) => {
      invalidateFinancialQueries(queryClient)
      queryClient.invalidateQueries({ queryKey: ['recurring'] })
      toast.success(t(skipped ? 'recurring.skip.done' : 'recurring.skip.undone'))
    },
    onError: (err: unknown) => toast.error(extractApiError(err, t('common.error'))),
  })

  const generateMutation = useMutation({
    mutationFn: () => recurringApi.generate(),
    onSuccess: (data) => {
      invalidateFinancialQueries(queryClient)
      queryClient.invalidateQueries({ queryKey: ['recurring'] })
      toast.success(t('recurring.generated', { count: data.generated }))
    },
    onError: () => toast.error(t('common.error')),
  })

  const frequencyLabel = (f: string) => {
    const map: Record<string, string> = {
      monthly: t('recurring.monthly'),
      quarterly: t('recurring.quarterly'),
      semiannual: t('recurring.semiannual'),
      weekly: t('recurring.weekly'),
      biweekly: t('recurring.biweekly'),
      yearly: t('recurring.yearly'),
      weekly_billed_monthly: t('recurring.weekly_billed_monthly'),
    }
    return map[f] ?? f
  }

  return (
    <>
      <SectionCard>
        <SectionHeader
          title={t('recurring.title')}
          action={
            canWrite ? (
              <div className="flex gap-2">
                {recurringList?.some((rt) => rt.auto_generate) && <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5 h-8"
                  onClick={() => generateMutation.mutate()}
                  disabled={generateMutation.isPending}
                >
                  <RefreshCw size={12} />
                  <span className="hidden sm:inline">{t('recurring.generatePending')}</span>
                </Button>}
                <Button size="sm" className="gap-1.5 h-8" onClick={() => { setEditing(null); setDraft(null); setDialogOpen(true) }}>
                  <Plus size={13} /> <span className="hidden sm:inline">{t('recurring.add')}</span>
                </Button>
              </div>
            ) : undefined
          }
        />
        {recurringList && recurringList.length > 0 ? (
          <table className="w-full">
            <thead>
              <tr className="border-b border-border">
                <th className={`${TH} pl-4 sm:pl-5 text-left`}>{t('recurring.description')}</th>
                <th className={`${TH} text-left w-36`}>{t('recurring.amount')}</th>
                <th className={`${TH} text-left w-28 hidden md:table-cell`}>{t('recurring.frequency')}</th>
                <th className={`${TH} text-left w-32 hidden md:table-cell`}>{t('recurring.nextOccurrence')}</th>
                <th className={`${TH} text-left w-24 hidden sm:table-cell`}>{t('recurring.status')}</th>
                {canWrite && <th className={`${TH} pr-4 sm:pr-5 text-right w-24`}>{t('recurring.actions')}</th>}
              </tr>
            </thead>
            <tbody>
              {recurringList.map((rt) => (
                <tr key={rt.id} className="border-b border-border last:border-0 hover:bg-muted transition-colors">
                  <td className="py-3 pl-4 sm:pl-5 text-sm font-medium text-foreground">
                    <div className="flex flex-wrap items-center gap-2">
                      <span>{rt.description}</span>
                      {(rt.extra_account_ids?.length ?? 0) > 0 && (
                        <span
                          className="text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground"
                          title={[rt.account_id, ...(rt.extra_account_ids ?? [])].map((id) => accountNames[id ?? ''] ?? '').filter(Boolean).join(', ')}
                          data-testid="recurring-accounts"
                        >
                          {t('recurring.accountsCount', { count: 1 + (rt.extra_account_ids?.length ?? 0) })}
                        </span>
                      )}
                      {(rt.alerts?.conflicts ?? 0) > 0 && (
                        <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-rose-500/10 text-rose-600" data-testid="recurring-conflicts">
                          {t('recurring.conflicts', { count: rt.alerts!.conflicts })}
                        </span>
                      )}
                    </div>
                    {rt.timeline && rt.timeline.length > 0 && (
                      <RecurrenceTimeline
                        slots={rt.timeline}
                        currency={rt.currency}
                        accountId={rt.account_id}
                        accountNames={accountNames}
                        onSkip={canWrite && !rt.auto_generate ? (period, skipped) => skipMutation.mutate({ id: rt.id, period, skipped }) : undefined}
                      />
                    )}
                    {rt.alerts?.suggest_end && rt.is_active && (
                      <div className="mt-1.5 flex flex-wrap items-center gap-2 rounded-md bg-amber-500/10 px-2 py-1 text-xs font-normal text-amber-700 dark:text-amber-400" data-testid="recurring-suggest-end">
                        <AlertTriangle size={12} className="shrink-0" />
                        <span>{t('recurring.suggestEnd', { count: rt.alerts.missed_streak })}</span>
                        {canWrite && (
                          <button
                            type="button"
                            className="font-semibold underline-offset-2 hover:underline"
                            onClick={() => endMutation.mutate(rt.id)}
                            disabled={endMutation.isPending}
                          >
                            {t('recurring.end')}
                          </button>
                        )}
                      </div>
                    )}
                  </td>
                  <td className={`py-3 text-xs sm:text-sm font-bold tabular-nums ${rt.type === 'credit' ? 'text-emerald-600' : 'text-rose-500'}`}>
                    {mask(`${rt.type === 'credit' ? '+' : '−'}${formatCurrency(rt.amount, rt.currency, locale)}`)}
                    {rt.currency !== userCurrency && rt.amount_primary != null && (
                      <div className="flex items-center gap-1 text-[11px] font-normal text-muted-foreground">
                        <span>{mask(formatCurrency(rt.amount_primary, userCurrency, locale))}</span>
                        <span title={t('recurring.fxEstimate', { rate: rt.fx_rate_used?.toFixed(4) ?? '–' })}>
                          <Info size={11} className="inline opacity-60" />
                        </span>
                      </div>
                    )}
                  </td>
                  <td className="py-3 hidden md:table-cell">
                    <span className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded-full font-medium">
                      {frequencyLabel(rt.frequency)}
                    </span>
                  </td>
                  <td className="py-3 text-xs text-muted-foreground tabular-nums hidden md:table-cell">
                    {new Date((rt.next?.expected_date ?? rt.next_occurrence) + 'T00:00:00').toLocaleDateString(dateLocale)}
                  </td>
                  <td className="py-3 hidden sm:table-cell">
                    <span className={cn(
                      'text-[11px] font-semibold px-2 py-0.5 rounded-full border',
                      rt.is_active
                        ? 'bg-emerald-50 text-emerald-600 border-emerald-100'
                        : 'bg-muted text-muted-foreground border-border'
                    )}>
                      {rt.is_active ? t('recurring.active') : t('recurring.inactive')}
                    </span>
                  </td>
                  {canWrite && (
                    <td className="py-3 pr-4 sm:pr-5">
                      <div className="flex items-center justify-end gap-1">
                        <button
                          className="p-1.5 rounded-md text-muted-foreground hover:text-primary hover:bg-primary/5 transition-colors"
                          onClick={() => { setEditing(rt); setDialogOpen(true) }}
                          aria-label={t('common.edit')}
                          title={t('common.edit')}
                        >
                          <Pencil size={13} />
                        </button>
                        <button
                          className="p-1.5 rounded-md text-muted-foreground hover:text-rose-500 hover:bg-rose-50 transition-colors"
                          onClick={() => setDeletingRecurring(rt)}
                          disabled={deleteMutation.isPending}
                          aria-label={t('common.delete')}
                          title={t('common.delete')}
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="text-sm text-muted-foreground text-center py-10">{t('recurring.empty')}</p>
        )}
      </SectionCard>

      {canWrite && (
        <RecurringSuggestions
          accounts={accountsList ?? []}
          onCreate={(suggestion) => {
            setEditing(null)
            setDraft({
              description: suggestion.description,
              amount: Number(suggestion.amount),
              currency: suggestion.currency,
              type: suggestion.type,
              frequency: suggestion.frequency,
              day_of_month: suggestion.day_of_month,
              day_window: suggestion.day_window,
              start_date: suggestion.start_date,
              account_id: suggestion.account_id,
              amount_mode: suggestion.amount_mode,
              amount_tolerance_pct: suggestion.amount_tolerance_pct,
              match_condition: suggestion.match_condition,
            })
            setDialogOpen(true)
          }}
        />
      )}

      <Dialog open={dialogOpen} onOpenChange={closeDialog}>
        <DialogContent className="sm:max-w-2xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editing ? t('recurring.edit') : t('recurring.add')}</DialogTitle>
          </DialogHeader>
          <RecurringForm
            key={editing?.id ?? (draft ? JSON.stringify(draft) : 'new')}
            recurring={editing}
            draft={draft}
            categories={categoriesList ?? []}
            categoryGroups={categoryGroupsList ?? []}
            currentCategory={allCategoriesList?.find(
              (category) => category.id === editing?.category_id
            )}
            accounts={accountsList ?? []}
            onSave={(data) => {
              if (editing) {
                updateMutation.mutate({ id: editing.id, ...data })
              } else {
                createMutation.mutate(data)
              }
            }}
            onCancel={closeDialog}
            loading={createMutation.isPending || updateMutation.isPending}
          />
        </DialogContent>
      </Dialog>

      <DeleteConfirmationDialog
        open={!!deletingRecurring}
        title={t('recurring.confirmDeleteTitle')}
        description={t('recurring.confirmDeleteDescription', { description: deletingRecurring?.description })}
        isPending={deleteMutation.isPending}
        onClose={() => setDeletingRecurring(null)}
        onConfirm={() => deletingRecurring && deleteMutation.mutate(deletingRecurring.id)}
      />
    </>
  )
}

/** Recurring charges detected in an account's history (RFC §8.5). */
function RecurringSuggestions({
  accounts,
  onCreate,
}: {
  accounts: Account[]
  onCreate: (suggestion: RecurringSuggestion) => void
}) {
  const { t, i18n } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const queryClient = useQueryClient()
  const candidates = useMemo(
    () => sortAccountsByDisplayName(accounts.filter((a) => !a.is_closed && a.type !== 'investment')),
    [accounts],
  )
  const [accountId, setAccountId] = useState('')
  const selected = accountId || candidates[0]?.id || ''

  const { data: suggestions } = useQuery({
    queryKey: ['recurring-suggestions', selected],
    queryFn: () => recurringApi.suggestions(selected),
    enabled: !!selected,
  })

  const dismissMutation = useMutation({
    mutationFn: (key: string) => recurringApi.dismissSuggestion(selected, key),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['recurring-suggestions', selected] }),
    onError: () => toast.error(t('common.error')),
  })

  if (candidates.length === 0) return null
  const frequencyLabel = (f: string) => t(`recurring.${f}`)

  return (
    <div className="mt-4">
      <SectionCard>
        <SectionHeader
          title={t('recurring.suggestions.title')}
          action={
            <select
              className="border border-border rounded-lg px-2 py-1 text-xs bg-card text-foreground"
              value={selected}
              onChange={(e) => setAccountId(e.target.value)}
              aria-label={t('recurring.account')}
            >
              {candidates.map((acc) => <option key={acc.id} value={acc.id}>{getAccountName(acc)}</option>)}
            </select>
          }
        />
        {suggestions && suggestions.length > 0 ? (
          <ul className="divide-y divide-border" data-testid="recurring-suggestions">
            {suggestions.map((s) => (
              <li key={s.key} className="flex flex-wrap items-center gap-3 px-4 sm:px-5 py-3">
                <Sparkles size={14} className="shrink-0 text-primary" />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-foreground truncate">{s.description}</p>
                  <p className="text-xs text-muted-foreground">
                    {frequencyLabel(s.frequency)}
                    {' · '}
                    {s.amount_mode === 'variable' ? t('recurring.suggestions.about') + ' ' : ''}
                    {mask(formatCurrency(Number(s.amount), s.currency, locale))}
                    {' · '}
                    {t('recurring.suggestions.seen', {
                      count: s.periods_covered,
                      last: s.occurrences.length
                        ? new Date(s.occurrences[s.occurrences.length - 1].date + 'T00:00:00').toLocaleDateString(i18n.language, { day: '2-digit', month: 'short' })
                        : '',
                    })}
                  </p>
                </div>
                <div className="flex gap-2">
                  <Button size="sm" className="h-7 text-xs" onClick={() => onCreate(s)}>{t('recurring.suggestions.create')}</Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 text-xs gap-1"
                    onClick={() => dismissMutation.mutate(s.key)}
                    disabled={dismissMutation.isPending}
                  >
                    <X size={12} /> {t('recurring.suggestions.dismiss')}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground text-center py-6">{t('recurring.suggestions.empty')}</p>
        )}
      </SectionCard>
    </div>
  )
}

function RecurringForm({
  recurring,
  draft,
  categories,
  categoryGroups,
  currentCategory,
  accounts,
  onSave,
  onCancel,
  loading,
}: {
  recurring: RecurringTransaction | null
  draft?: Partial<RecurringTransaction> | null
  categories: Category[]
  categoryGroups: CategoryGroup[]
  currentCategory?: Category
  accounts: { id: string; name: string; display_name?: string | null; type?: string; is_closed?: boolean }[]
  onSave: (data: Partial<RecurringTransaction>) => void
  onCancel: () => void
  loading: boolean
}) {
  const { t, i18n } = useTranslation()
  const locale = useDisplayLocale()
  const { user } = useAuth()
  const userCurrency = user?.preferences?.currency_display ?? 'USD'
  const src: Partial<RecurringTransaction> | null = recurring ?? draft ?? null
  const sortedAccounts = useMemo(() => sortAccountsByDisplayName(accounts), [accounts])
  const { data: supportedCurrencies } = useQuery({
    queryKey: ['currencies'],
    queryFn: currenciesApi.list,
    staleTime: Infinity,
  })
  const [description, setDescription] = useState(src?.description ?? '')
  const [amount, setAmount] = useState(src?.amount?.toString() ?? '')
  const [currency, setCurrency] = useState(src?.currency ?? userCurrency)
  const [type, setType] = useState<'debit' | 'credit'>(src?.type ?? 'debit')
  const [frequency, setFrequency] = useState(src?.frequency ?? 'monthly')
  const [weekendAdjustment, setWeekendAdjustment] = useState<RecurringTransaction['weekend_adjustment']>(
    src?.weekend_adjustment ?? 'none'
  )
  const [dayOfMonth, setDayOfMonth] = useState(src?.day_of_month?.toString() ?? '')
  const [startDate, setStartDate] = useState(src?.start_date ?? localDateString())
  const [endDate, setEndDate] = useState(src?.end_date ?? '')
  const [categoryId, setCategoryId] = useState(src?.category_id ?? '')
  const [accountId, setAccountId] = useState(src?.account_id ?? sortedAccounts[0]?.id ?? '')
  const [extraAccountIds, setExtraAccountIds] = useState<string[]>(src?.extra_account_ids ?? [])
  const extraCandidates = sortedAccounts.filter((a) => a.id !== accountId && !a.is_closed)
  const [isActive, setIsActive] = useState(recurring?.is_active ?? true)
  // New recurrences are forecast from their timeline (RFC §8.8); the
  // placeholder generator stays only for recurrences that already use it.
  const [autoGenerate, setAutoGenerate] = useState(recurring?.auto_generate ?? false)
  const showAutoGenerate = !!recurring?.auto_generate
  const [amountMode, setAmountMode] = useState<'fixed' | 'variable'>(src?.amount_mode ?? 'fixed')
  const [tolerance, setTolerance] = useState(String(src?.amount_tolerance_pct ?? '0'))
  const [dayWindow, setDayWindow] = useState(src?.day_window?.toString() ?? '')
  const [billingWeekday, setBillingWeekday] = useState(String(src?.billing_weekday ?? 0))
  const [weekBasis, setWeekBasis] = useState<'previous_month' | 'same_month'>(src?.week_count_basis ?? 'previous_month')
  const [conditionTree, setConditionTree] = useState(() => toConditionTree(src?.match_condition))
  const isWeeklyBilled = frequency === 'weekly_billed_monthly'
  const selectedAccount = sortedAccounts.find((a) => a.id === accountId)
  const conditionScope = selectedAccount?.type === 'credit_card' ? 'credit_card' : selectedAccount ? 'bank' : null

  const weekdayNames = useMemo(
    () => Array.from({ length: 7 }, (_, i) =>
      // 2024-01-01 was a Monday: weekday 0 in the backend's convention.
      new Date(Date.UTC(2024, 0, 1 + i)).toLocaleDateString(i18n.language, { weekday: 'long', timeZone: 'UTC' })),
    [i18n.language],
  )
  // Live preview of the next bill (AC-6.8): "October: 5 × R$150 = R$750".
  const weeklyPreview = useMemo(() => {
    if (!isWeeklyBilled) return null
    const perWeek = parseFloat(amount)
    if (!Number.isFinite(perWeek)) return null
    const today = new Date()
    const { units, year, month } = billedUnits(today.getFullYear(), today.getMonth() + 1, Number(billingWeekday), weekBasis)
    const monthName = new Date(year, month - 1, 1).toLocaleDateString(i18n.language, { month: 'long' })
    return t('recurring.weeklyPreview', {
      month: monthName,
      units,
      amount: formatCurrency(perWeek, currency, locale),
      total: formatCurrency(perWeek * units, currency, locale),
    })
  }, [isWeeklyBilled, amount, billingWeekday, weekBasis, currency, locale, i18n.language, t])

  const selectClass = 'w-full border border-border rounded-lg px-3 py-2 text-sm bg-card text-foreground focus:outline-none focus:ring-2 focus:ring-primary'

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        onSave({
          description,
          amount: parseFloat(amount),
          currency,
          type,
          frequency,
          weekend_adjustment: weekendAdjustment,
          day_of_month: dayOfMonth ? parseInt(dayOfMonth) : null,
          start_date: startDate,
          end_date: endDate || null,
          category_id: categoryId || null,
          account_id: accountId || null,
          extra_account_ids: autoGenerate ? [] : extraAccountIds.filter((id) => id !== accountId),
          is_active: isActive,
          auto_generate: autoGenerate,
          amount_mode: amountMode,
          amount_tolerance_pct: tolerance === '' ? 0 : parseFloat(tolerance),
          day_window: dayWindow === '' ? null : parseInt(dayWindow),
          billing_weekday: isWeeklyBilled ? Number(billingWeekday) : null,
          week_count_basis: isWeeklyBilled ? weekBasis : null,
          match_condition: conditionTree.conditions.length
            ? { op: conditionTree.op, conditions: conditionTree.conditions }
            : null,
        } as Partial<RecurringTransaction>)
      }}
      className="space-y-4"
    >
      <div className="space-y-2">
        <Label>{t('recurring.description')}</Label>
        <Input value={description} onChange={(e) => setDescription(e.target.value)} required />
      </div>
      <div className="grid grid-cols-3 gap-4">
        <div className="space-y-2">
          <Label>{isWeeklyBilled ? t('recurring.amountPerWeek') : t('recurring.amount')}</Label>
          <Input type="number" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} required />
        </div>
        <div className="space-y-2">
          <Label>{t('recurring.currency')}</Label>
          <select className={selectClass} value={currency} onChange={(e) => setCurrency(e.target.value)}>
            {(supportedCurrencies ?? [{ code: userCurrency, symbol: userCurrency, name: userCurrency, flag: '' }]).map((c) => (
              <option key={c.code} value={c.code}>{c.flag} {c.name}</option>
            ))}
          </select>
        </div>
        <div className="space-y-2">
          <Label>{t('recurring.type')}</Label>
          <select className={selectClass} value={type} onChange={(e) => setType(e.target.value as 'debit' | 'credit')}>
            <option value="debit">{t('recurring.expense')}</option>
            <option value="credit">{t('recurring.income')}</option>
          </select>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-2">
          <Label>{t('recurring.frequency')}</Label>
          <select className={selectClass} value={frequency} onChange={(e) => setFrequency(e.target.value as RecurringTransaction['frequency'])}>
            <option value="monthly">{t('recurring.monthly')}</option>
            <option value="quarterly">{t('recurring.quarterly')}</option>
            <option value="semiannual">{t('recurring.semiannual')}</option>
            <option value="weekly">{t('recurring.weekly')}</option>
            <option value="biweekly">{t('recurring.biweekly')}</option>
            <option value="yearly">{t('recurring.yearly')}</option>
            <option value="weekly_billed_monthly">{t('recurring.weekly_billed_monthly')}</option>
          </select>
        </div>
        {(frequency === 'monthly' || frequency === 'quarterly' || frequency === 'semiannual' || isWeeklyBilled) && (
          <div className="space-y-2">
            <Label>{isWeeklyBilled ? t('recurring.billingDay') : t('recurring.dayOfMonth')}</Label>
            <Input type="number" min="1" max="31" value={dayOfMonth} onChange={(e) => setDayOfMonth(e.target.value)} required={isWeeklyBilled} />
          </div>
        )}
      </div>
      {isWeeklyBilled && (
        <div className="space-y-2 rounded-lg border border-border p-3">
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label>{t('recurring.billingWeekday')}</Label>
              <select className={selectClass} value={billingWeekday} onChange={(e) => setBillingWeekday(e.target.value)}>
                {weekdayNames.map((name, i) => <option key={i} value={i}>{name}</option>)}
              </select>
            </div>
            <div className="space-y-2">
              <Label>{t('recurring.weekCountBasis')}</Label>
              <select className={selectClass} value={weekBasis} onChange={(e) => setWeekBasis(e.target.value as 'previous_month' | 'same_month')}>
                <option value="previous_month">{t('recurring.basisPreviousMonth')}</option>
                <option value="same_month">{t('recurring.basisSameMonth')}</option>
              </select>
            </div>
          </div>
          {weeklyPreview && <p className="text-xs text-muted-foreground" data-testid="weekly-preview">{weeklyPreview}</p>}
        </div>
      )}
      <div className="grid grid-cols-3 gap-4">
        <div className="space-y-2">
          <Label>{t('recurring.amountMode')}</Label>
          <select className={selectClass} value={amountMode} onChange={(e) => setAmountMode(e.target.value as 'fixed' | 'variable')}>
            <option value="fixed">{t('recurring.amountFixed')}</option>
            <option value="variable">{t('recurring.amountVariable')}</option>
          </select>
        </div>
        <div className="space-y-2">
          <Label>{t('recurring.tolerance')}</Label>
          <Input type="number" min="0" max="100" step="0.1" value={tolerance} onChange={(e) => setTolerance(e.target.value)} />
        </div>
        <div className="space-y-2">
          <Label>{t('recurring.dayWindow')}</Label>
          <Input type="number" min="0" max="31" value={dayWindow} onChange={(e) => setDayWindow(e.target.value)} placeholder={t('recurring.dayWindowAuto')} />
        </div>
      </div>
      <div className="space-y-2">
        <Label>{t('recurring.matchCondition')}</Label>
        <p className="text-xs text-muted-foreground">{t('recurring.matchConditionHint')}</p>
        <ConditionTreeEditor value={conditionTree} onChange={setConditionTree} scope={conditionScope} accounts={sortedAccounts} />
      </div>
      <div className="space-y-2">
        <Label>{t('recurring.weekendAdjustment')}</Label>
        <select
          className={selectClass}
          value={weekendAdjustment}
          onChange={(e) => setWeekendAdjustment(e.target.value as RecurringTransaction['weekend_adjustment'])}
        >
          <option value="none">{t('recurring.weekendAdjustmentNone')}</option>
          <option value="previous_friday">{t('recurring.weekendAdjustmentPreviousFriday')}</option>
          <option value="next_monday">{t('recurring.weekendAdjustmentNextMonday')}</option>
        </select>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-2">
          <Label>{t('recurring.startDate')}</Label>
          <DatePickerInput value={startDate} onChange={setStartDate} className="w-full justify-start" />
        </div>
        <div className="space-y-2">
          <Label>{t('recurring.endDate')}</Label>
          <DatePickerInput value={endDate} onChange={setEndDate} placeholder={t('recurring.endDate')} className="w-full justify-start" />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-2">
          <Label>{t('recurring.category')}</Label>
          <CategorySelect
            value={categoryId}
            onChange={setCategoryId}
            categories={categories}
            groups={categoryGroups}
            currentCategory={currentCategory}
            allowNone={true}
            className={selectClass}
          />
        </div>
        <div className="space-y-2">
          <Label>{t('recurring.account')}</Label>
          <select
            className={selectClass}
            value={accountId}
            onChange={(e) => setAccountId(e.target.value)}
            required
          >
            {!accountId && <option value="" disabled>{t('recurring.noAccount')}</option>}
            {sortedAccounts.map((acc) => (
              <option key={acc.id} value={acc.id}>{getAccountName(acc)}</option>
            ))}
          </select>
        </div>
      </div>
      {!autoGenerate && extraCandidates.length > 0 && (
        <div className="space-y-2" data-testid="extra-accounts">
          <Label>{t('recurring.extraAccounts')}</Label>
          <p className="text-xs text-muted-foreground">{t('recurring.extraAccountsHint')}</p>
          <div className="flex flex-wrap gap-x-4 gap-y-1.5">
            {extraCandidates.map((acc) => (
              <label key={acc.id} className="flex items-center gap-2 cursor-pointer text-sm text-foreground">
                <input
                  type="checkbox"
                  checked={extraAccountIds.includes(acc.id)}
                  onChange={(e) => setExtraAccountIds((ids) => e.target.checked ? [...ids, acc.id] : ids.filter((id) => id !== acc.id))}
                  className="h-4 w-4 rounded border-border"
                />
                {getAccountName(acc)}
              </label>
            ))}
          </div>
        </div>
      )}
      {showAutoGenerate && <label className="flex items-start gap-2 cursor-pointer">
        <input
          type="checkbox"
          checked={autoGenerate}
          onChange={(e) => setAutoGenerate(e.target.checked)}
          className="h-4 w-4 mt-0.5 rounded border-border"
        />
        <span className="text-sm text-foreground">
          {t('recurring.autoGenerate')}
          <span className="block text-xs text-muted-foreground">{t('recurring.autoGenerateHelp')}</span>
        </span>
      </label>}
      {recurring && (
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={isActive}
            onChange={(e) => setIsActive(e.target.checked)}
            className="h-4 w-4 rounded border-border"
          />
          <span className="text-sm text-foreground">{t('recurring.active')}</span>
        </label>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="submit" disabled={loading}>
          {loading ? t('common.loading') : t('common.save')}
        </Button>
      </DialogFooter>
    </form>
  )
}
