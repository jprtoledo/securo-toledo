import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useInfiniteQuery } from '@tanstack/react-query'
import { getAccountName, sortAccountsByDisplayName } from '@/lib/account-utils'
import { VALUELESS_ACTIONS, isInvalidDescriptionAction, parseRulePriority, previewableActions } from '@/lib/rule-form-utils'
import { rules as rulesApi } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import { useDisplayLocale, useDateLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import { X, Plus, ChevronDown, Eye, ArrowRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { CategorySelect } from '@/components/category-select'
import {
  MAX_CONDITION_DEPTH,
  flattenConditions,
  isConditionGroup,
  isConditionNot,
  nodeDepth,
} from '@/lib/rule-conditions'
import {
  CARD_LINE_KINDS,
  LIST_OPS,
  OPERATION_TYPES,
  VALUELESS_OPS,
  actionsForScope,
  defaultValueForField,
  fieldDef,
  fieldsForScope,
  isBlankConditionValue,
  opsForField,
  parseListValue,
} from '@/lib/rule-fields'
import type {
  Category,
  CategoryGroup,
  Payee,
  Rule,
  RuleCondition,
  RuleConditionNode,
  RuleAction,
  RuleScopeKind,
} from '@/types'

function newCondition(): RuleCondition {
  return { field: 'description', op: 'contains', value: '' }
}

/** Apply one edit to a leaf, resetting op/value when the field or the kind
 *  of operator (single value / list / none) changes. */
function applyLeafChange(
  condition: RuleCondition,
  key: keyof RuleCondition,
  val: RuleCondition['value'],
): RuleCondition {
  if (key === 'field') {
    return {
      field: String(val),
      op: opsForField(String(val))[0].value,
      value: defaultValueForField(String(val)),
    }
  }
  if (key === 'op') {
    const op = String(val)
    if (VALUELESS_OPS.has(op)) return { field: condition.field, op }
    if (LIST_OPS.has(op)) {
      const current = condition.value
      const list = Array.isArray(current) ? current : current ? [current] : []
      return { ...condition, op, value: list }
    }
    const current = Array.isArray(condition.value) ? condition.value[0] ?? '' : condition.value ?? ''
    return { ...condition, op, value: current === '' ? defaultValueForField(condition.field) : current }
  }
  return { ...condition, [key]: val }
}

type NodePath = number[]

function childrenOf(node: RuleConditionNode): RuleConditionNode[] {
  if (isConditionGroup(node)) return node.conditions
  if (isConditionNot(node)) return [node.not]
  return []
}

function withChildren(node: RuleConditionNode, children: RuleConditionNode[]): RuleConditionNode {
  if (isConditionGroup(node)) return { ...node, conditions: children }
  if (isConditionNot(node)) return { not: children[0] }
  return node
}

/** Replace the node at `path` (`null` removes it). Emptied groups and
 *  negations disappear with their last child — the API rejects them. */
function updateAt(
  nodes: RuleConditionNode[],
  path: NodePath,
  fn: (node: RuleConditionNode) => RuleConditionNode | null,
): RuleConditionNode[] {
  const [head, ...rest] = path
  return nodes.flatMap((node, i) => {
    if (i !== head) return [node]
    if (rest.length === 0) {
      const next = fn(node)
      return next ? [next] : []
    }
    const children = updateAt(childrenOf(node), rest, fn)
    return children.length ? [withChildren(node, children)] : []
  })
}

const SELECT_CLASS = 'border border-border rounded-lg px-2 py-1.5 text-sm bg-card text-foreground focus:outline-none focus:ring-2 focus:ring-primary'

/** AND/OR switch, used for the rule itself and for each nested group. */
function OpToggle({ value, onChange }: { value: 'and' | 'or'; onChange: (op: 'and' | 'or') => void }) {
  const { t } = useTranslation()
  return (
    <div className="flex items-center gap-1 bg-muted rounded-lg p-0.5">
      {(['and', 'or'] as const).map(op => (
        <button
          key={op}
          type="button"
          className={cn(
            'px-3 py-1 text-xs font-semibold rounded-md transition-all',
            value === op ? 'bg-card shadow-sm text-foreground' : 'text-muted-foreground hover:text-foreground'
          )}
          onClick={() => onChange(op)}
        >
          {op === 'and' ? t('rules.andOp') : t('rules.orOp')}
        </button>
      ))}
    </div>
  )
}

interface ConditionContext {
  scope: RuleScopeKind | null
  accounts: { id: string; name: string; display_name?: string | null }[]
  payees: Payee[]
}

function ConditionValueInput({
  condition, ctx, onChange,
}: {
  condition: RuleCondition
  ctx: ConditionContext
  onChange: (val: RuleCondition['value']) => void
}) {
  const { t } = useTranslation()
  const def = fieldDef(condition.field)
  const valueClass = `${SELECT_CLASS} col-span-2 w-full min-w-0 sm:w-0 sm:flex-1`
  if (VALUELESS_OPS.has(condition.op)) return <span className="hidden sm:block sm:flex-1" />
  if (LIST_OPS.has(condition.op)) {
    const text = Array.isArray(condition.value) ? condition.value.join(', ') : String(condition.value ?? '')
    return (
      <Input
        className="col-span-2 h-8 w-full min-w-0 text-sm sm:w-0 sm:flex-1"
        defaultValue={text}
        onBlur={(e) => onChange(parseListValue(e.target.value))}
        onChange={(e) => onChange(parseListValue(e.target.value))}
        placeholder={t('rules.listValuePlaceholder')}
      />
    )
  }
  if (def?.type === 'enum' && def.options && condition.field !== 'operation_type') {
    return (
      <select className={valueClass} value={String(condition.value ?? '')} onChange={(e) => onChange(e.target.value)}>
        {def.options.map(o => <option key={o.value} value={o.value}>{t(o.label)}</option>)}
      </select>
    )
  }
  if (def?.type === 'account') {
    return (
      <select className={valueClass} value={String(condition.value ?? '')} onChange={(e) => onChange(e.target.value)}>
        <option value="">{t('rules.selectAccount')}</option>
        {ctx.accounts.map(acc => <option key={acc.id} value={acc.id}>{getAccountName(acc)}</option>)}
      </select>
    )
  }
  if (def?.type === 'payee') {
    return (
      <select className={valueClass} value={String(condition.value ?? '')} onChange={(e) => onChange(e.target.value)}>
        <option value="">{t('rules.selectPayee')}</option>
        {ctx.payees.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select>
    )
  }
  const listId = condition.field === 'operation_type' ? 'rule-operation-types' : undefined
  return (
    <>
      <Input
        className="col-span-2 h-8 w-full min-w-0 text-sm sm:w-0 sm:flex-1"
        value={String(condition.value ?? '')}
        onChange={(e) => onChange(e.target.value)}
        list={listId}
        placeholder={def?.type === 'number' ? '0.00' : def?.type === 'date' ? 'YYYY-MM-DD' : t('rules.valuePlaceholder')}
        type={def?.type === 'number' ? 'number' : def?.type === 'date' ? 'date' : 'text'}
      />
      {listId && (
        <datalist id={listId}>
          {OPERATION_TYPES.map(v => <option key={v} value={v}>{v}</option>)}
        </datalist>
      )}
    </>
  )
}

function ConditionRow({
  condition, ctx, onChange, onRemove, onNegate,
}: {
  condition: RuleCondition
  ctx: ConditionContext
  onChange: (key: keyof RuleCondition, val: RuleCondition['value']) => void
  onRemove: () => void
  onNegate?: () => void
}) {
  const { t } = useTranslation()
  const fields = fieldsForScope(ctx.scope)
  const known = fields.some(f => f.value === condition.field)
  return (
    <div className="relative grid min-w-0 grid-cols-2 gap-2 pr-12 sm:flex sm:items-center sm:pr-0">
      <select
        className={`${SELECT_CLASS} w-full sm:w-36 sm:shrink-0`}
        value={condition.field}
        onChange={(e) => onChange('field', e.target.value)}
        aria-label={t('rules.field')}
      >
        {!known && <option value={condition.field}>{condition.field}</option>}
        {fields.map(f => <option key={f.value} value={f.value}>{t(f.label)}</option>)}
      </select>
      <select
        className={`${SELECT_CLASS} w-full sm:w-32 sm:shrink-0`}
        value={condition.op}
        onChange={(e) => onChange('op', e.target.value)}
        aria-label={t('rules.operator')}
      >
        {!opsForField(condition.field).some(o => o.value === condition.op) && (
          <option value={condition.op}>{condition.op}</option>
        )}
        {opsForField(condition.field).map(o => <option key={o.value} value={o.value}>{t(o.label)}</option>)}
      </select>
      <ConditionValueInput condition={condition} ctx={ctx} onChange={(val) => onChange('value', val)} />
      <div className="absolute right-0 top-1 flex shrink-0 items-center sm:static">
        {onNegate && (
          <button
            type="button"
            className="p-1 text-[10px] font-bold text-muted-foreground transition-colors hover:text-foreground"
            title={t('rules.negateHint')}
            onClick={onNegate}
          >
            {t('rules.notOp')}
          </button>
        )}
        <button
          type="button"
          className="p-1 text-muted-foreground transition-colors hover:text-rose-500"
          onClick={onRemove}
          aria-label={t('common.delete')}
        >
          <X size={13} />
        </button>
      </div>
    </div>
  )
}

/** One node of the condition tree, recursively: leaf, group or negation. */
function ConditionNodeEditor({
  node, path, depth, ctx, update,
}: {
  node: RuleConditionNode
  path: NodePath
  depth: number
  ctx: ConditionContext
  update: (path: NodePath, fn: (node: RuleConditionNode) => RuleConditionNode | null) => void
}) {
  const { t } = useTranslation()
  const remove = () => update(path, () => null)
  const negate = () => update(path, (n) => ({ not: n }))
  const canNest = depth < MAX_CONDITION_DEPTH

  if (isConditionNot(node)) {
    return (
      <div className="rounded-lg border border-dashed border-rose-300/70 p-2 space-y-2 dark:border-rose-800/70">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-semibold text-rose-600 dark:text-rose-400">{t('rules.notGroup')}</span>
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground"
            onClick={() => update(path, (n) => (isConditionNot(n) ? n.not : n))}
          >
            {t('rules.removeNot')}
          </button>
        </div>
        <ConditionNodeEditor node={node.not} path={[...path, 0]} depth={depth + 1} ctx={ctx} update={update} />
      </div>
    )
  }
  if (isConditionGroup(node)) {
    return (
      <div className="rounded-lg border border-border bg-muted/40 p-2 space-y-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-medium text-muted-foreground">{t('rules.group')}</span>
          <div className="flex items-center gap-1">
            <OpToggle
              value={node.op}
              onChange={(op) => update(path, (n) => (isConditionGroup(n) ? { ...n, op } : n))}
            />
            {canNest && (
              <button
                type="button"
                className="px-1 text-[10px] font-bold text-muted-foreground hover:text-foreground"
                title={t('rules.negateHint')}
                onClick={negate}
              >
                {t('rules.notOp')}
              </button>
            )}
            <button
              type="button"
              className="shrink-0 p-1 text-muted-foreground transition-colors hover:text-rose-500"
              title={t('rules.removeGroup')}
              onClick={remove}
            >
              <X size={13} />
            </button>
          </div>
        </div>
        {node.conditions.map((child, j) => (
          <ConditionNodeEditor key={j} node={child} path={[...path, j]} depth={depth + 1} ctx={ctx} update={update} />
        ))}
        <div className="flex flex-wrap items-center gap-4">
          <button
            type="button"
            className="text-xs text-primary hover:text-primary/80 font-medium flex items-center gap-1"
            onClick={() => update(path, (n) => (isConditionGroup(n) ? { ...n, conditions: [...n.conditions, newCondition()] } : n))}
          >
            <Plus size={12} /> {t('rules.addCondition')}
          </button>
          {depth + 1 < MAX_CONDITION_DEPTH && (
            <button
              type="button"
              className="text-xs text-primary hover:text-primary/80 font-medium flex items-center gap-1"
              onClick={() => update(path, (n) => (isConditionGroup(n)
                ? { ...n, conditions: [...n.conditions, { op: 'or', conditions: [newCondition(), newCondition()] }] }
                : n))}
            >
              <Plus size={12} /> {t('rules.addGroup')}
            </button>
          )}
        </div>
      </div>
    )
  }
  return (
    <ConditionRow
      condition={node}
      ctx={ctx}
      onChange={(key, val) => update(path, (n) => applyLeafChange(n as RuleCondition, key, val))}
      onRemove={remove}
      onNegate={canNest ? negate : undefined}
    />
  )
}

/** A standalone condition tree (root AND/OR + nodes), used by the
 *  recurrence form to describe which charges belong to it (RFC §8.2). */
export function ConditionTreeEditor({
  value, onChange, scope = null, accounts = [], payees = [],
}: {
  value: { op: 'and' | 'or'; conditions: RuleConditionNode[] }
  onChange: (value: { op: 'and' | 'or'; conditions: RuleConditionNode[] }) => void
  scope?: RuleScopeKind | null
  accounts?: { id: string; name: string; display_name?: string | null }[]
  payees?: Payee[]
}) {
  const { t } = useTranslation()
  const ctx: ConditionContext = { scope, accounts, payees }
  const update = (path: NodePath, fn: (node: RuleConditionNode) => RuleConditionNode | null) =>
    onChange({ ...value, conditions: updateAt(value.conditions, path, fn) })
  return (
    <div className="space-y-2">
      {value.conditions.length > 1 && (
        <div className="flex justify-end">
          <OpToggle value={value.op} onChange={(op) => onChange({ ...value, op })} />
        </div>
      )}
      {value.conditions.map((node, i) => (
        <ConditionNodeEditor key={i} node={node} path={[i]} depth={2} ctx={ctx} update={update} />
      ))}
      <div className="flex flex-wrap items-center gap-4">
        <button
          type="button"
          className="text-xs text-primary hover:text-primary/80 font-medium flex items-center gap-1"
          onClick={() => onChange({ ...value, conditions: [...value.conditions, newCondition()] })}
        >
          <Plus size={12} /> {t('rules.addCondition')}
        </button>
        <button
          type="button"
          className="text-xs text-primary hover:text-primary/80 font-medium flex items-center gap-1"
          onClick={() => onChange({
            ...value,
            conditions: [...value.conditions, { op: 'or', conditions: [newCondition(), newCondition()] }],
          })}
        >
          <Plus size={12} /> {t('rules.addGroup')}
        </button>
      </div>
    </div>
  )
}

/** Rows per preview request. Each one re-evaluates the whole ledger, so the
 * page is large enough that reading through a broad rule's matches is a few
 * requests rather than dozens. The API caps it at 100. */
const PREVIEW_PAGE_SIZE = 50

/** Collapsible "what would this rule do?" panel.
 *
 * Matching runs on the backend against the same engine that applies rules, so
 * the table shows exactly what saving the draft would produce — including the
 * transactions it matches but leaves untouched because they already have a
 * category. The counts cover every match; the table is one window of them at a
 * time, so a broad rule — the kind this panel exists to catch before it is
 * saved — can be read through rather than judged by its first screenful. Any
 * edit to the draft collapses the panel rather than leaving a stale table on
 * screen.
 */
function RulePreviewPanel({
  conditionsOp, conditions, actions, isActive, applyToExisting, overwriteExistingCategories,
  disabled, open, onOpenChange, scopeKind = null, scopeAccountId = null,
}: {
  scopeKind?: RuleScopeKind | null
  scopeAccountId?: string | null
  conditionsOp: 'and' | 'or'
  conditions: RuleConditionNode[]
  actions: RuleAction[]
  // The save-time flags go to the backend too: an inactive rule, or one not
  // being applied to existing transactions, changes nothing when saved, and
  // the preview has to say so rather than promise changes that won't happen.
  isActive: boolean
  applyToExisting: boolean
  overwriteExistingCategories: boolean
  disabled: boolean
  // Open state lives in the parent: the dialog widens while the table is
  // expanded, so both have to react to the same toggle.
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const { mask } = usePrivacyMode()

  // A half-filled action row is a draft in progress, not a rule to reject, so
  // it is left out of the request the backend validates.
  const draftActions = useMemo(() => previewableActions(actions), [actions])

  // Keyed on the whole draft, so flipping a flag refetches while the panel
  // stays open — and a slower response for a previous draft can never land on
  // top of the current one. Paged, because a rule matching four figures of
  // transactions is one worth reading past the first page of.
  const preview = useInfiniteQuery({
    queryKey: [
      'rule-preview', conditionsOp, conditions, draftActions,
      isActive, applyToExisting, overwriteExistingCategories, scopeKind, scopeAccountId,
    ],
    queryFn: ({ pageParam }) => rulesApi.preview({
      conditions_op: conditionsOp,
      conditions,
      actions: draftActions,
      is_active: isActive,
      apply_to_existing: applyToExisting,
      overwrite_existing_categories: overwriteExistingCategories,
      scope_account_kind: scopeKind,
      scope_account_id: scopeAccountId,
      limit: PREVIEW_PAGE_SIZE,
      offset: pageParam,
    }),
    initialPageParam: 0,
    // The counts are exact whatever window came back, so what is already on
    // screen is the offset of the next page.
    getNextPageParam: (lastPage, pages) => {
      const shown = pages.reduce((total, page) => total + page.sample.length, 0)
      return shown < lastPage.matched ? shown : undefined
    },
    enabled: open,
    staleTime: Infinity,
    gcTime: 0,
  })

  // Editing the rule itself collapses the panel rather than leaving a table
  // that describes a draft the user has moved on from. The flags don't: their
  // whole point is watching the numbers move.
  useEffect(() => {
    onOpenChange(false)
  }, [conditionsOp, conditions, actions, onOpenChange])

  // Every page carries the same counts; the rows accumulate.
  const data = preview.data?.pages[0]
  const sample = useMemo(
    () => preview.data?.pages.flatMap(page => page.sample) ?? [],
    [preview.data],
  )

  return (
    <div className="rounded-lg border border-border">
      <button
        type="button"
        disabled={disabled}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-sm text-foreground transition-colors hover:bg-muted/60 disabled:cursor-not-allowed disabled:opacity-50"
        onClick={() => onOpenChange(!open)}
      >
        <span className="flex items-center gap-1.5 font-medium">
          <Eye size={13} /> {t('rules.preview')}
        </span>
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          {data && t('rules.previewMatched', { matched: data.matched })}
          <ChevronDown size={14} className={cn('transition-transform', open && 'rotate-180')} />
        </span>
      </button>

      {open && (
        <div className="border-t border-border p-3">
          {preview.isError ? (
            <p className="text-xs text-rose-500">{t('rules.previewError')}</p>
          ) : /* also while a flag change is being recomputed: the old numbers
                 no longer describe the flags now on screen. Fetching a further
                 page is not that — those rows are appended to a table that is
                 still current. */
          !data || (preview.isFetching && !preview.isFetchingNextPage) ? (
            <p className="text-xs text-muted-foreground">{t('common.loading')}</p>
          ) : data.matched === 0 ? (
            <p className="text-xs text-muted-foreground">{t('rules.previewEmpty')}</p>
          ) : (
            <div className="space-y-2">
              <p className="text-xs text-muted-foreground">
                {t('rules.previewSummary', { matched: data.matched, changed: data.will_change })}
                {!data.will_apply && (
                  <> · <span className="font-medium text-amber-600 dark:text-amber-400">
                    {isActive ? t('rules.previewNotAppliedToExisting') : t('rules.previewInactive')}
                  </span></>
                )}
                {sample.length < data.matched && (
                  <> · {t('rules.previewSampleNote', { shown: sample.length })}</>
                )}
              </p>
              <div className="max-h-56 overflow-y-auto overflow-x-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-card text-muted-foreground">
                    <tr className="border-b border-border text-left">
                      <th className="py-1.5 pr-2 font-medium">{t('transactions.date')}</th>
                      <th className="py-1.5 pr-2 font-medium">{t('transactions.description')}</th>
                      <th className="py-1.5 pr-2 text-right font-medium">{t('transactions.amount')}</th>
                      <th className="py-1.5 font-medium">{t('transactions.category')}</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {sample.map(item => (
                      <tr key={item.id} className={cn(!item.will_change && 'text-muted-foreground')}>
                        <td className="whitespace-nowrap py-1.5 pr-2 tabular-nums">
                          {new Date(item.date + 'T00:00:00').toLocaleDateString(dateLocale)}
                        </td>
                        <td className="max-w-[22rem] truncate py-1.5 pr-2" title={item.description}>
                          {item.description}
                        </td>
                        <td className={cn(
                          'whitespace-nowrap py-1.5 pr-2 text-right tabular-nums',
                          item.will_change && item.type === 'credit' && 'text-emerald-600',
                        )}>
                          {mask(formatCurrency(Math.abs(item.amount), item.currency, locale))}
                        </td>
                        <td className="py-1.5">
                          {item.will_change ? (
                            <span className="flex items-center gap-1">
                              <span className="truncate">
                                {item.current_category_name ?? t('transactions.uncategorized')}
                              </span>
                              <ArrowRight size={11} className="shrink-0 text-muted-foreground" />
                              <span className="truncate font-medium text-emerald-600">
                                {item.new_category_name ?? t('transactions.uncategorized')}
                              </span>
                            </span>
                          ) : (
                            <span className="flex items-center gap-1">
                              <span className="truncate">
                                {item.current_category_name ?? t('transactions.uncategorized')}
                              </span>
                              <span className="shrink-0 rounded-full bg-muted px-1.5 text-[10px] font-semibold">
                                {t('rules.previewNoChange')}
                              </span>
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {preview.hasNextPage && (
                <button
                  type="button"
                  className="w-full rounded-md border border-border py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-muted/60 disabled:cursor-not-allowed disabled:opacity-50"
                  disabled={preview.isFetchingNextPage}
                  onClick={() => preview.fetchNextPage()}
                >
                  {preview.isFetchingNextPage
                    ? t('common.loading')
                    : t('rules.previewLoadMore', {
                        more: Math.min(PREVIEW_PAGE_SIZE, data.matched - sample.length),
                      })}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export interface RuleDialogInitialData {
  name?: string
  conditions?: RuleConditionNode[]
  actions?: RuleAction[]
  applyToExisting?: boolean
  overwriteExistingCategories?: boolean
}

export function RuleDialog({
  open, onClose, rule, categories, categoryGroups, currentCategories = [], accounts, payees, onSave, loading, initialData,
}: {
  open: boolean
  onClose: () => void
  rule: Rule | null
  categories: Category[]
  categoryGroups: CategoryGroup[]
  currentCategories?: Category[]
  accounts: { id: string; name: string; display_name?: string | null; type?: string }[]
  payees: Payee[]
  onSave: (data: Partial<Rule>) => void
  loading: boolean
  initialData?: RuleDialogInitialData
}) {
  const { t } = useTranslation()
  const sortedAccounts = useMemo(() => sortAccountsByDisplayName(accounts), [accounts])

  const defaultConditions: RuleConditionNode[] = initialData?.conditions ?? rule?.conditions ?? [newCondition()]
  const defaultActions: RuleAction[] = initialData?.actions ?? rule?.actions as RuleAction[] ?? [{ op: 'set_category', value: '' }]

  const [name, setName] = useState(initialData?.name ?? rule?.name ?? '')
  const [conditionsOp, setConditionsOp] = useState<'and' | 'or'>(rule?.conditions_op ?? 'and')
  const [conditions, setConditions] = useState<RuleConditionNode[]>(
    defaultConditions.length ? defaultConditions : [newCondition()]
  )
  const [actions, setActions] = useState<RuleAction[]>(
    defaultActions.length ? defaultActions : [{ op: 'set_category', value: '' }]
  )
  const [priority, setPriority] = useState(String(rule?.priority ?? 0))
  // Read, never written here: the row owns the switch. Kept so an edit
  // preserves the rule's own state instead of resetting it to on, and so
  // the preview can still say "this will match, but the rule is off".
  const [isActive] = useState(rule?.is_active ?? true)
  const [applyToExisting, setApplyToExisting] = useState(initialData?.applyToExisting ?? !rule)
  const [overwriteExistingCategories, setOverwriteExistingCategories] = useState(
    initialData?.overwriteExistingCategories ?? false
  )
  const [previewOpen, setPreviewOpen] = useState(false)
  // Scope (Rules v2): every account, bank accounts, credit cards, or one
  // account — which also decides the card-only fields and actions offered.
  const [scopeKind, setScopeKind] = useState<RuleScopeKind | null>(rule?.scope_account_kind ?? null)
  const [scopeAccountId, setScopeAccountId] = useState<string | null>(rule?.scope_account_id ?? null)
  const scopeAccount = accounts.find(a => a.id === scopeAccountId) as
    | { id: string; type?: string }
    | undefined
  const effectiveScope: RuleScopeKind | null = scopeAccountId
    ? (scopeAccount?.type === 'credit_card' ? 'credit_card' : 'bank')
    : scopeKind
  const conditionContext: ConditionContext = { scope: effectiveScope, accounts: sortedAccounts, payees }

  const updateNode = (path: NodePath, fn: (node: RuleConditionNode) => RuleConditionNode | null) => {
    setConditions(prev => updateAt(prev, path, fn))
  }

  function addCondition() {
    setConditions(prev => [...prev, newCondition()])
  }

  // A group starts with two conditions: one alone would carry no AND/OR meaning.
  function addGroup() {
    setConditions(prev => [...prev, { op: 'or', conditions: [newCondition(), newCondition()] }])
  }

  function updateAction(i: number, field: keyof RuleAction, val: string) {
    setActions(prev => prev.map((a, idx) => {
      if (idx !== i) return a
      const next = { ...a, [field]: val }
      if (field === 'op') {
        next.value = val === 'set_card_line_kind' ? 'payment' : VALUELESS_ACTIONS.has(val) ? null : ''
      }
      return next
    }))
  }

  function removeAction(i: number) {
    setActions(prev => prev.filter((_, idx) => idx !== i))
  }

  function addAction() {
    setActions(prev => [...prev, { op: 'set_category', value: '' }])
  }

  // A blank condition value matches every transaction, so the rule would apply
  // its actions to the whole ledger. The API rejects these too.
  const hasBlankCondition = flattenConditions(conditions).some(c => isBlankConditionValue(c.op, c.value))
  const tooDeep = conditions.some(node => nodeDepth(node) > MAX_CONDITION_DEPTH)
  const hasInvalidDescriptionAction = actions.some(isInvalidDescriptionAction)

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    // Dialog renders its content through a portal, but React still bubbles
    // events along the component tree rather than the DOM tree, so without
    // this the submit would also reach the transaction form wrapping this
    // dialog and trigger an unrelated save of it.
    e.stopPropagation()
    if (hasBlankCondition || hasInvalidDescriptionAction || tooDeep) return
    onSave({
      name,
      conditions_op: conditionsOp,
      conditions,
      actions,
      priority: parseRulePriority(priority),
      is_active: isActive,
      scope_account_kind: scopeAccountId ? null : scopeKind,
      scope_account_id: scopeAccountId,
      apply_to_existing: applyToExisting,
      overwrite_existing_categories: applyToExisting && overwriteExistingCategories,
    })
  }

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent
        aria-describedby={undefined}
        className={cn(
          // The preview table needs room to breathe, so the dialog widens while
          // it is expanded — same idiom as the transaction dialog's preview pane.
          'max-h-[90vh] overflow-y-auto overflow-x-hidden transition-[max-width] duration-300',
          previewOpen ? 'sm:max-w-5xl max-w-2xl' : 'sm:max-w-2xl max-w-2xl',
        )}
      >
        <DialogHeader>
          <DialogTitle>{rule ? t('rules.editRule') : t('rules.newRule')}</DialogTitle>
        </DialogHeader>
        <form key={rule?.id ?? 'new'} onSubmit={handleSubmit} className="space-y-5">
          {/* Name + Priority */}
          <div className="grid grid-cols-3 gap-3">
            <div className="col-span-2 space-y-1.5">
              <Label>{t('rules.name')}</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} required placeholder="Ex: Uber" />
            </div>
            <div className="space-y-1.5">
              <Label>{t('rules.priority')}</Label>
              <Input
                type="number"
                step="1"
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
                onBlur={() => {
                  if (priority.trim() === '' || !Number.isFinite(Number(priority))) {
                    setPriority('0')
                  }
                }}
              />
            </div>
          </div>

          {/* Scope */}
          <div className="space-y-1.5">
            <Label htmlFor="rule-scope">{t('rules.scope')}</Label>
            <select
              id="rule-scope"
              className={`${SELECT_CLASS} w-full`}
              value={scopeAccountId ? `account:${scopeAccountId}` : scopeKind ?? ''}
              onChange={(e) => {
                const v = e.target.value
                if (v.startsWith('account:')) {
                  setScopeAccountId(v.slice('account:'.length))
                  setScopeKind(null)
                } else {
                  setScopeAccountId(null)
                  setScopeKind((v || null) as RuleScopeKind | null)
                }
              }}
            >
              <option value="">{t('rules.scopeAll')}</option>
              <option value="bank">{t('rules.scopeBank')}</option>
              <option value="credit_card">{t('rules.scopeCard')}</option>
              <optgroup label={t('rules.scopeOneAccount')}>
                {sortedAccounts.map(acc => (
                  <option key={acc.id} value={`account:${acc.id}`}>{getAccountName(acc)}</option>
                ))}
              </optgroup>
            </select>
            <p className="text-xs text-muted-foreground">{t('rules.scopeHint')}</p>
          </div>

          {/* Conditions */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>{t('rules.conditions')}</Label>
              <OpToggle value={conditionsOp} onChange={setConditionsOp} />
            </div>
            <div className="space-y-2">
              {conditions.map((node, i) => (
                <ConditionNodeEditor
                  key={i}
                  node={node}
                  path={[i]}
                  depth={1}
                  ctx={conditionContext}
                  update={updateNode}
                />
              ))}
              {hasBlankCondition && (
                <p className="text-xs text-rose-500">{t('rules.blankConditionValue')}</p>
              )}
              {tooDeep && (
                <p className="text-xs text-rose-500">{t('rules.tooDeep', { max: MAX_CONDITION_DEPTH })}</p>
              )}
              <div className="flex flex-wrap items-center gap-4">
                <button
                  type="button"
                  className="text-xs text-primary hover:text-primary/80 font-medium flex items-center gap-1"
                  onClick={addCondition}
                >
                  <Plus size={12} /> {t('rules.addCondition')}
                </button>
                <button
                  type="button"
                  className="text-xs text-primary hover:text-primary/80 font-medium flex items-center gap-1"
                  title={t('rules.addGroupHint')}
                  onClick={addGroup}
                >
                  <Plus size={12} /> {t('rules.addGroup')}
                </button>
              </div>
            </div>
          </div>

          {/* Actions */}
          <div className="space-y-2">
            <Label>{t('rules.actions')}</Label>
            <div className="space-y-2">
              {actions.map((action, i) => {
                const invalidDescription = isInvalidDescriptionAction(action)
                return (
                  <div key={i} className="space-y-1">
                    <div className="relative grid min-w-0 gap-2 pr-7 sm:flex sm:items-center sm:pr-0">
                      <select
                        className={`${SELECT_CLASS} w-full sm:w-40 sm:shrink-0`}
                        value={action.op}
                        onChange={(e) => updateAction(i, 'op', e.target.value)}
                      >
                        {!actionsForScope(effectiveScope).some(a => a.value === action.op) && (
                          <option value={action.op}>{action.op}</option>
                        )}
                        {actionsForScope(effectiveScope).map(a => (
                          <option key={a.value} value={a.value}>{t(a.label)}</option>
                        ))}
                      </select>
                      {action.op === 'ignore' ? (
                        <span className="min-w-0 text-sm italic text-muted-foreground sm:w-0 sm:flex-1">
                          {t('rules.ignoreActionHint')}
                        </span>
                      ) : action.op === 'clear_category' ? (
                        <span className="min-w-0 text-sm italic text-muted-foreground sm:w-0 sm:flex-1">
                          {t('rules.clearCategoryHint')}
                        </span>
                      ) : action.op === 'set_card_line_kind' ? (
                        <select
                          className={`${SELECT_CLASS} w-full min-w-0 sm:w-0 sm:flex-1`}
                          value={action.value ?? 'payment'}
                          onChange={(e) => updateAction(i, 'value', e.target.value)}
                        >
                          {CARD_LINE_KINDS.map(kind => (
                            <option key={kind} value={kind}>{t(`rules.cardKind.${kind}`)}</option>
                          ))}
                        </select>
                      ) : action.op === 'set_category' ? (
                        <div className="w-full min-w-0 sm:w-0 sm:flex-1">
                          <CategorySelect
                            value={action.value ?? ''}
                            onChange={(val) => updateAction(i, 'value', val)}
                            categories={categories}
                            groups={categoryGroups}
                            currentCategory={currentCategories.find(
                              (category) => category.id === action.value
                            )}
                            placeholder={t('rules.selectCategory')}
                            creatable
                            className={`${SELECT_CLASS} w-full`}
                          />
                        </div>
                      ) : action.op === 'set_payee' ? (
                        <select
                          className={`${SELECT_CLASS} w-full min-w-0 sm:w-0 sm:flex-1`}
                          value={action.value ?? ''}
                          onChange={(e) => updateAction(i, 'value', e.target.value)}
                          required
                        >
                          <option value="">{t('rules.selectPayee')}</option>
                          {payees.map(p => (
                            <option key={p.id} value={p.id}>{p.name}</option>
                          ))}
                        </select>
                      ) : (
                        <Input
                          className="h-8 w-full min-w-0 text-sm aria-invalid:border-input aria-invalid:ring-0 dark:aria-invalid:ring-0 sm:w-0 sm:flex-1"
                          value={action.value ?? ''}
                          onChange={(e) => updateAction(i, 'value', e.target.value)}
                          placeholder={
                            action.op === 'set_description'
                              ? t('rules.descriptionValuePlaceholder')
                              : action.op === 'strip_installment_marker'
                                ? t('rules.stripPatternPlaceholder')
                                : t('rules.notesValuePlaceholder')
                          }
                          maxLength={action.op === 'set_description' ? 500 : undefined}
                          required={action.op === 'set_description'}
                          aria-invalid={invalidDescription || undefined}
                          aria-describedby={invalidDescription ? `action-${i}-description-error` : undefined}
                        />
                      )}
                      <button
                        type="button"
                        className="absolute right-0 top-1 shrink-0 p-1 text-muted-foreground transition-colors hover:text-rose-500 sm:static"
                        onClick={() => removeAction(i)}
                      >
                        <X size={13} />
                      </button>
                    </div>
                    {invalidDescription && (
                      <p id={`action-${i}-description-error`} className="text-xs text-rose-500">
                        {t('rules.invalidDescriptionValue')}
                      </p>
                    )}
                  </div>
                )
              })}
              <button
                type="button"
                className="text-xs text-primary hover:text-primary/80 font-medium flex items-center gap-1"
                onClick={addAction}
              >
                <Plus size={12} /> {t('rules.addAction')}
              </button>
            </div>
          </div>

          {/* No "rule is active" box here any more. Whether a rule runs
              is a property of the rule as it sits in the list, not of
              the form you edit it in, and having it here meant turning
              one off was: open it, scroll, find a checkbox, save a form
              you did not otherwise want to touch. It is a switch on the
              row now. `isActive` is still carried and sent back
              untouched, so editing a stopped rule does not quietly
              restart it. */}

          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={applyToExisting}
              onChange={(e) => setApplyToExisting(e.target.checked)}
              className="h-4 w-4 rounded border-border"
            />
            <span className="text-sm text-foreground">
              {t('rules.applyToExisting', 'Apply matching actions to existing transactions')}
            </span>
          </label>

          {applyToExisting && (
            <label className="flex items-center gap-2 cursor-pointer pl-6">
              <input
                type="checkbox"
                checked={overwriteExistingCategories}
                onChange={(e) => setOverwriteExistingCategories(e.target.checked)}
                className="h-4 w-4 rounded border-border"
              />
              <span className="text-sm text-foreground">
                {t('rules.overwriteManualChoices')}
              </span>
            </label>
          )}

          <RulePreviewPanel
            conditionsOp={conditionsOp}
            conditions={conditions}
            actions={actions}
            isActive={isActive}
            applyToExisting={applyToExisting}
            overwriteExistingCategories={overwriteExistingCategories}
            disabled={hasBlankCondition || tooDeep || conditions.length === 0}
            scopeKind={scopeAccountId ? null : scopeKind}
            scopeAccountId={scopeAccountId}
            open={previewOpen}
            onOpenChange={setPreviewOpen}
          />

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="submit" disabled={loading || hasBlankCondition || tooDeep}>
              {loading ? t('common.loading') : t('common.save')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
