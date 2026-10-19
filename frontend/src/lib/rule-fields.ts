/**
 * Field and operator catalog of Rules v2 (RFC toledo-features §5.3).
 * Mirrors `app/services/rule_engine.py` — the backend is the authority and
 * rejects anything this list lets through by mistake.
 */
import type { RuleScopeKind } from '../types'

export type FieldType = 'string' | 'number' | 'date' | 'enum' | 'account' | 'payee'

export interface RuleFieldDef {
  value: string
  label: string
  type: FieldType
  /** Only exists on credit-card rows: needs a card scope. */
  cardOnly?: boolean
  /** Fields from before Rules v2 keep their original operator set. */
  legacy?: boolean
  options?: { value: string; label: string }[]
}

export const OPERATION_TYPES = [
  'PAGAMENTO_FATURA', 'PAGAMENTO', 'TARIFA', 'ESTORNO', 'CASHBACK',
  'OPERACOES_CREDITO_CONTRATADAS_CARTAO', 'PIX', 'TED', 'DOC', 'BOLETO',
  'TRANSFERENCIA_MESMA_INSTITUICAO', 'CONVENIO_ARRECADACAO', 'FOLHA_PAGAMENTO',
  'DEPOSITO', 'SAQUE', 'CARTAO', 'RENDIMENTO_APLIC_FINANCEIRA', 'RESGATE_APLIC_FINANCEIRA',
  'OPERACAO_CREDITO', 'ENCARGOS_JUROS_CHEQUE_ESPECIAL', 'PACOTE_TARIFA_SERVICOS',
  'TARIFA_SERVICOS_AVULSOS', 'PORTABILIDADE_SALARIO', 'OUTROS',
]

export const RULE_FIELDS: RuleFieldDef[] = [
  { value: 'description', label: 'rules.fieldDescription', type: 'string', legacy: true },
  { value: 'description_key', label: 'rules.fieldDescriptionKey', type: 'string' },
  { value: 'original_description', label: 'rules.fieldOriginalDescription', type: 'string' },
  { value: 'provider_description_raw', label: 'rules.fieldProviderDescriptionRaw', type: 'string' },
  { value: 'merchant_name', label: 'rules.fieldMerchant', type: 'string' },
  { value: 'payee', label: 'rules.fieldRawPayee', type: 'string', legacy: true },
  { value: 'notes', label: 'rules.fieldNotes', type: 'string', legacy: true },
  { value: 'amount', label: 'rules.fieldAmount', type: 'number', legacy: true },
  { value: 'signed_amount', label: 'rules.fieldSignedAmount', type: 'number' },
  { value: 'currency', label: 'rules.fieldCurrency', type: 'string' },
  {
    value: 'type', label: 'rules.fieldType', type: 'enum', legacy: true,
    options: [{ value: 'debit', label: 'rules.typeExpense' }, { value: 'credit', label: 'rules.typeIncome' }],
  },
  {
    value: 'status', label: 'rules.fieldStatus', type: 'enum',
    options: [{ value: 'pending', label: 'rules.statusPending' }, { value: 'posted', label: 'rules.statusPosted' }],
  },
  {
    value: 'operation_type', label: 'rules.fieldOperationType', type: 'enum',
    options: OPERATION_TYPES.map(v => ({ value: v, label: v })),
  },
  { value: 'provider_category', label: 'rules.fieldProviderCategory', type: 'string' },
  {
    value: 'account_kind', label: 'rules.fieldAccountKind', type: 'enum',
    options: [
      { value: 'bank', label: 'rules.scopeBank' },
      { value: 'credit_card', label: 'rules.scopeCard' },
    ],
  },
  { value: 'account_id', label: 'rules.fieldAccount', type: 'account', legacy: true },
  { value: 'payee_id', label: 'rules.fieldPayee', type: 'payee', legacy: true },
  { value: 'date', label: 'rules.fieldDate', type: 'date', legacy: true },
  { value: 'card_number', label: 'rules.fieldCardNumber', type: 'string', cardOnly: true },
  { value: 'installment_number', label: 'rules.fieldInstallmentNumber', type: 'number', cardOnly: true },
  { value: 'total_installments', label: 'rules.fieldTotalInstallments', type: 'number', cardOnly: true },
  {
    value: 'payment_type', label: 'rules.fieldPaymentType', type: 'enum', cardOnly: true,
    options: [
      { value: 'SINGLE', label: 'rules.paymentSingle' },
      { value: 'INSTALLMENT', label: 'rules.paymentInstallment' },
    ],
  },
]

export interface OpDef { value: string; label: string }

const TEXT_OPS: OpDef[] = [
  { value: 'contains', label: 'rules.opContains' },
  { value: 'not_contains', label: 'rules.opNotContains' },
  { value: 'equals', label: 'rules.opEquals' },
  { value: 'not_equals', label: 'rules.opNotEquals' },
  { value: 'starts_with', label: 'rules.opStartsWith' },
  { value: 'ends_with', label: 'rules.opEndsWith' },
  { value: 'regex', label: 'rules.opRegex' },
  { value: 'not_regex', label: 'rules.opNotRegex' },
  { value: 'in', label: 'rules.opIn' },
  { value: 'not_in', label: 'rules.opNotIn' },
  { value: 'is_empty', label: 'rules.opIsEmpty' },
  { value: 'is_not_empty', label: 'rules.opIsNotEmpty' },
]
const NUMBER_OPS: OpDef[] = [
  { value: 'equals', label: '=' },
  { value: 'not_equals', label: '≠' },
  { value: 'gt', label: '>' },
  { value: 'gte', label: '>=' },
  { value: 'lt', label: '<' },
  { value: 'lte', label: '<=' },
  { value: 'in', label: 'rules.opIn' },
  { value: 'not_in', label: 'rules.opNotIn' },
  { value: 'is_empty', label: 'rules.opIsEmpty' },
  { value: 'is_not_empty', label: 'rules.opIsNotEmpty' },
]
const DATE_OPS: OpDef[] = NUMBER_OPS.filter(o => !['in', 'not_in', 'is_empty', 'is_not_empty'].includes(o.value))
const ENUM_OPS: OpDef[] = [
  { value: 'equals', label: 'rules.opIs' },
  { value: 'not_equals', label: 'rules.opIsNot' },
  { value: 'in', label: 'rules.opIn' },
  { value: 'not_in', label: 'rules.opNotIn' },
  { value: 'is_empty', label: 'rules.opIsEmpty' },
  { value: 'is_not_empty', label: 'rules.opIsNotEmpty' },
]
const ID_OPS: OpDef[] = ENUM_OPS.slice(0, 2)

export const VALUELESS_OPS = new Set(['is_empty', 'is_not_empty'])
export const LIST_OPS = new Set(['in', 'not_in'])

export function fieldDef(field: string): RuleFieldDef | undefined {
  return RULE_FIELDS.find(f => f.value === field)
}

export function opsForField(field: string): OpDef[] {
  const def = fieldDef(field)
  switch (def?.type) {
    case 'number': return NUMBER_OPS
    case 'date': return DATE_OPS
    case 'enum': return field === 'type' ? ENUM_OPS.slice(0, 2) : ENUM_OPS
    case 'account':
    case 'payee': return ID_OPS
    default: return TEXT_OPS
  }
}

/** Fields offered for a scope: card-only fields need a card scope. */
export function fieldsForScope(scope: RuleScopeKind | null | undefined): RuleFieldDef[] {
  return RULE_FIELDS.filter(f => !f.cardOnly || scope === 'credit_card')
}

export function defaultValueForField(field: string): string {
  const def = fieldDef(field)
  return def?.type === 'enum' && def.options?.length ? def.options[0].value : ''
}

/** Blank values match everything, so the API refuses them (value-less
 *  operators carry none; list operators need at least one item). */
export function isBlankConditionValue(op: string, value: unknown): boolean {
  if (VALUELESS_OPS.has(op)) return false
  if (LIST_OPS.has(op)) {
    const items = Array.isArray(value) ? value : String(value ?? '').split(',')
    return items.filter(v => String(v).trim() !== '').length === 0
  }
  return String(value ?? '').trim() === ''
}

/** "a, b , c" → ["a", "b", "c"] for in/not_in. */
export function parseListValue(text: string): string[] {
  return text.split(',').map(v => v.trim()).filter(Boolean)
}

export const CARD_LINE_KINDS = ['purchase', 'credit', 'payment', 'previous_balance'] as const

export const RULE_ACTIONS: { value: string; label: string; cardOnly?: boolean }[] = [
  { value: 'set_category', label: 'rules.setCategory' },
  { value: 'clear_category', label: 'rules.clearCategory' },
  { value: 'set_description', label: 'rules.setDescription' },
  { value: 'set_payee', label: 'rules.setPayee' },
  { value: 'append_notes', label: 'rules.appendNotes' },
  { value: 'ignore', label: 'rules.ignoreAction' },
  { value: 'set_card_line_kind', label: 'rules.setCardLineKind', cardOnly: true },
  { value: 'strip_installment_marker', label: 'rules.stripInstallmentMarker', cardOnly: true },
]

export function actionsForScope(scope: RuleScopeKind | null | undefined) {
  return RULE_ACTIONS.filter(a => !a.cardOnly || scope === 'credit_card')
}
