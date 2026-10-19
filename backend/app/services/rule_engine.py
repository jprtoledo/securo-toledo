"""Pure rule evaluation engine — no DB access."""
import re
import unicodedata
import uuid
from collections.abc import Collection
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.transaction import Transaction


def _strip_accents(text: str) -> str:
    """Remove diacritics (accents), preserving case."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def compile_rule_regex(pattern: str) -> re.Pattern[str]:
    """Compile a regex using Securo's runtime normalization and safety policy."""
    effective_pattern = _strip_accents(pattern)
    try:
        compiled = re.compile(effective_pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError("Invalid regular expression") from exc
    if compiled.search("") is not None:
        raise ValueError("Regular expression must not match an empty string")
    return compiled


def _normalize(text: str) -> str:
    """Normalize text: uppercase and remove diacritics (accents)."""
    return _strip_accents(text.upper())


def _to_decimal(val) -> Decimal:
    try:
        return Decimal(str(val))
    except InvalidOperation:
        return Decimal("0")


def _to_date(val) -> date | None:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        try:
            return date.fromisoformat(val)
        except ValueError:
            return None
    return None


# --- Field catalog (RFC toledo-features §5.3) --------------------------------

ACCOUNT_KIND_BANK = "bank"
ACCOUNT_KIND_CARD = "credit_card"
ACCOUNT_KIND_OTHER = "other"
ACCOUNT_KINDS = (ACCOUNT_KIND_BANK, ACCOUNT_KIND_CARD)

# Fields that existed before Rules v2 keep accepting every operator they
# accepted then, so a saved rule matches exactly what it matched (AC-3.1).
LEGACY_FIELDS = {
    "description", "payee", "notes", "amount", "type", "account_id", "payee_id", "date",
}
LEGACY_OPS = {
    "contains", "not_contains", "equals", "not_equals", "starts_with",
    "ends_with", "regex", "gt", "gte", "lt", "lte",
}
LIST_OPS = {"in", "not_in"}
VALUELESS_OPS = {"is_empty", "is_not_empty"}
STRING_OPS = {
    "contains", "not_contains", "starts_with", "ends_with", "equals", "not_equals",
    "regex", "not_regex", *LIST_OPS, *VALUELESS_OPS,
}
NUMBER_OPS = {"equals", "not_equals", "gt", "gte", "lt", "lte", *LIST_OPS, *VALUELESS_OPS}
ENUM_OPS = {"equals", "not_equals", *LIST_OPS, *VALUELESS_OPS}
ALL_OPS = LEGACY_OPS | STRING_OPS | NUMBER_OPS

# field -> type. Types: string, number, date, enum.
FIELD_TYPES: dict[str, str] = {
    "description": "string",
    "payee": "string",
    "notes": "string",
    "amount": "number",
    "type": "string",
    "account_id": "string",
    "payee_id": "string",
    "date": "date",
    "status": "enum",
    "description_key": "string",
    "original_description": "string",
    "provider_description_raw": "string",
    "merchant_name": "string",
    "signed_amount": "number",
    "currency": "string",
    "operation_type": "enum",
    "provider_category": "string",
    "card_number": "string",
    "installment_number": "number",
    "total_installments": "number",
    "payment_type": "enum",
    "account_kind": "enum",
}
# Only meaningful on credit-card rows: a rule using them must be scoped to cards.
CARD_ONLY_FIELDS = {"card_number", "installment_number", "total_installments", "payment_type"}
ENUM_VALUES: dict[str, tuple[str, ...] | None] = {
    "status": ("pending", "posted"),
    "payment_type": ("SINGLE", "INSTALLMENT"),
    "account_kind": (ACCOUNT_KIND_BANK, ACCOUNT_KIND_CARD, ACCOUNT_KIND_OTHER),
    # Open Finance operation types; free text, the list is what the editor offers.
    "operation_type": None,
}
OPERATION_TYPES = (
    "PAGAMENTO_FATURA", "PAGAMENTO", "TARIFA", "ESTORNO", "CASHBACK",
    "OPERACOES_CREDITO_CONTRATADAS_CARTAO", "PIX", "TED", "DOC", "BOLETO",
    "TRANSFERENCIA_MESMA_INSTITUICAO", "CONVENIO_ARRECADACAO", "FOLHA_PAGAMENTO",
    "DEPOSITO", "SAQUE", "CARTAO", "RENDIMENTO_APLIC_FINANCEIRA",
    "RESGATE_APLIC_FINANCEIRA", "OPERACAO_CREDITO", "ENCARGOS_JUROS_CHEQUE_ESPECIAL",
    "PACOTE_TARIFA_SERVICOS", "TARIFA_SERVICOS_AVULSOS", "PORTABILIDADE_SALARIO", "OUTROS",
)
CARD_LINE_KINDS = ("purchase", "credit", "payment", "previous_balance")
MAX_DEPTH = 5


def allowed_ops(field: str) -> set[str]:
    if field in LEGACY_FIELDS:
        return LEGACY_OPS | LIST_OPS | VALUELESS_OPS | {"not_regex"}
    kind = FIELD_TYPES.get(field)
    if kind == "number":
        return NUMBER_OPS
    if kind == "enum":
        return ENUM_OPS
    return STRING_OPS


def account_kind_of(account_type: str | None) -> str:
    if account_type in ("checking", "savings"):
        return ACCOUNT_KIND_BANK
    if account_type == "credit_card":
        return ACCOUNT_KIND_CARD
    return ACCOUNT_KIND_OTHER


def description_key(text: str | None) -> str:
    """Letters only, uppercased, accents stripped: "APPLE.COM/BILL 123" and
    "Apple.com/bill" both become ``APPLECOMBILL``."""
    return re.sub(r"[^A-Z]+", "", _normalize(text or ""))


def _field_value(tx, field: str, account_type: str | None):
    if field == "description_key":
        return description_key(getattr(tx, "description", None))
    if field == "signed_amount":
        amount = getattr(tx, "amount", None)
        if amount is None:
            return None
        amount = _to_decimal(amount)
        return amount if getattr(tx, "type", None) == "credit" else -amount
    if field == "account_kind":
        return account_kind_of(account_type) if account_type else None
    return getattr(tx, field, None)


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _norm_for(field: str, value) -> str:
    if field == "description_key":
        return description_key(str(value))
    return _normalize(str(value))


def _match_new_field(field: str, op: str, value, tx_val) -> bool:
    """Typed evaluation for the fields added by Rules v2."""
    kind = FIELD_TYPES[field]
    if op in LIST_OPS:
        items = value if isinstance(value, list) else [value]
        if tx_val is None or tx_val == "":
            return op == "not_in"
        if kind == "number":
            found = _to_decimal(tx_val) in {_to_decimal(v) for v in items}
        else:
            found = _norm_for(field, tx_val) in {_norm_for(field, v) for v in items}
        return found if op == "in" else not found
    if tx_val is None or (isinstance(tx_val, str) and tx_val == ""):
        return op in ("not_equals", "not_contains", "not_regex")
    if kind == "number":
        a, b = _to_decimal(tx_val), _to_decimal(value)
        return {
            "equals": a == b, "not_equals": a != b, "gt": a > b,
            "gte": a >= b, "lt": a < b, "lte": a <= b,
        }.get(op, False)
    actual = _norm_for(field, tx_val)
    if op in ("regex", "not_regex"):
        try:
            found = compile_rule_regex(str(value)).search(_normalize(str(tx_val))) is not None
        except ValueError:
            return False
        return found if op == "regex" else not found
    expected = _norm_for(field, value)
    if op == "equals":
        return actual == expected
    if op == "not_equals":
        return actual != expected
    if op == "contains":
        return expected in actual
    if op == "not_contains":
        return expected not in actual
    if op == "starts_with":
        return actual.startswith(expected)
    if op == "ends_with":
        return actual.endswith(expected)
    return False


def _match_condition(condition: dict, tx: "Transaction", account_type: str | None = None) -> bool:
    field = condition.get("field", "")
    op = condition.get("op", "")
    value = condition.get("value")

    tx_val = _field_value(tx, field, account_type)

    if op in VALUELESS_OPS:
        empty = _is_blank(tx_val)
        return empty if op == "is_empty" else not empty

    # A blank value matches everything: "" is a substring of any string, every
    # string starts/ends with it, an empty regex always matches, and numeric
    # comparisons fall back to 0. Creation now rejects these, but rules saved
    # before that validation existed are still in users' databases — refuse to
    # match rather than recategorizing their whole ledger. Explicit 0/False
    # are real values and pass through.
    if value is None or (isinstance(value, str) and not value.strip()):
        return False
    if isinstance(value, list) and (op not in LIST_OPS or not value):
        return False

    if field not in LEGACY_FIELDS:
        if field not in FIELD_TYPES:
            return False
        return _match_new_field(field, op, value, tx_val)

    if op in LIST_OPS:
        items = value if isinstance(value, list) else [value]
        found = _normalize(str(tx_val or "")) in {_normalize(str(v)) for v in items}
        return found if op == "in" else not found
    if op == "not_regex":
        try:
            return compile_rule_regex(str(value)).search(_normalize(str(tx_val or ""))) is None
        except ValueError:
            return False

    # String operators
    if op in ("contains", "not_contains", "starts_with", "ends_with", "equals", "not_equals", "regex"):
        tx_str = _normalize(str(tx_val or ""))
        val_str = _normalize(str(value or ""))

        if op == "contains":
            return val_str in tx_str
        if op == "not_contains":
            return val_str not in tx_str
        if op == "starts_with":
            return tx_str.startswith(val_str)
        if op == "ends_with":
            return tx_str.endswith(val_str)
        if op == "equals":
            return tx_str == val_str
        if op == "not_equals":
            return tx_str != val_str
        if op == "regex":
            try:
                # Strip accents from the pattern so it lines up with the
                # normalized text, but keep its case: uppercasing a regex
                # inverts escape classes (\s -> \S, \b -> \B, \d -> \D) and
                # breaks inline flags. Case is already handled by IGNORECASE.
                compiled = compile_rule_regex(str(value or ""))
                return compiled.search(tx_str) is not None
            except ValueError:
                return False

    # Numeric operators
    if op in ("gt", "gte", "lt", "lte"):
        if field == "date":
            tx_date = _to_date(tx_val)
            val_date = _to_date(value)
            if tx_date is None or val_date is None:
                return False
            if op == "gt":
                return tx_date > val_date
            if op == "gte":
                return tx_date >= val_date
            if op == "lt":
                return tx_date < val_date
            if op == "lte":
                return tx_date <= val_date

        tx_num = _to_decimal(tx_val)
        val_num = _to_decimal(value)
        if op == "gt":
            return tx_num > val_num
        if op == "gte":
            return tx_num >= val_num
        if op == "lt":
            return tx_num < val_num
        if op == "lte":
            return tx_num <= val_num

    return False


def _is_group(node: dict) -> bool:
    """A condition list entry is a group when it carries its own condition list."""
    return isinstance(node, dict) and isinstance(node.get("conditions"), list)


def _is_not(node: dict) -> bool:
    return isinstance(node, dict) and "not" in node and isinstance(node.get("not"), dict)


def evaluate_node(node: dict, tx: "Transaction", account_type: str | None = None, depth: int = 1) -> bool:
    """One condition node: a leaf ``{field, op, value}``, a group
    ``{op: "and"|"or", conditions: [...]}`` or a negation ``{not: node}``,
    nested to any depth up to MAX_DEPTH (deeper never matches)."""
    if depth > MAX_DEPTH:
        return False
    if _is_not(node):
        return not evaluate_node(node["not"], tx, account_type, depth + 1)
    if _is_group(node):
        conditions = node.get("conditions") or []
        if not conditions:
            return False
        results = (evaluate_node(c, tx, account_type, depth + 1) for c in conditions)
        if node.get("op") == "or":
            return any(results)
        return all(results)  # "and" is default
    return _match_condition(node, tx, account_type)


def evaluate_conditions(
    conditions_op: str,
    conditions: list[dict],
    tx: "Transaction",
    account_type: str | None = None,
) -> bool:
    """Return True if the transaction matches the rule's conditions.

    The top level is ``conditions_op`` + ``conditions``; each entry is a node
    (see ``evaluate_node``), so a rule can mix AND, OR and NOT — e.g.
    ``type is debit AND NOT (description_key starts with UBER OR merchant in
    [99, Cabify])``. ``account_type`` feeds the ``account_kind`` field.
    """
    if not conditions:
        return False
    results = (evaluate_node(node, tx, account_type) for node in conditions)
    if conditions_op == "or":
        return any(results)
    return all(results)  # "and" is default


# --- Scope -------------------------------------------------------------------


def rule_in_scope(rule, account_id, account_type: str | None) -> bool:
    """A rule scoped to an account kind or one account only sees its rows."""
    scope_account_id = getattr(rule, "scope_account_id", None)
    if scope_account_id is not None and scope_account_id != account_id:
        return False
    scope_kind = getattr(rule, "scope_account_kind", None)
    if scope_kind is not None and account_kind_of(account_type) != scope_kind:
        return False
    return True


# --- Validation (pure) ---------------------------------------------------------

ACTION_OPS = {
    "set_category", "clear_category", "set_payee", "set_description", "append_notes",
    "ignore", "set_card_line_kind", "strip_installment_marker",
}
CARD_ONLY_ACTIONS = {"set_card_line_kind", "strip_installment_marker"}
CATEGORY_ACTIONS = {"set_category", "clear_category"}
# Evaluated by recompute every time, never written into user-owned fields.
DERIVED_ACTIONS = {"set_card_line_kind", "strip_installment_marker"}


def _err(code: str, message: str, **params) -> dict:
    return {"code": code, "message": message, "params": params}


def _get(item, key):
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def _node_errors(node, scope_kind: str | None, depth: int, out: list[dict]) -> None:
    if depth > MAX_DEPTH:
        out.append(_err("too_deep", f"Conditions can be nested at most {MAX_DEPTH} levels", max=MAX_DEPTH))
        return
    if hasattr(node, "model_dump"):
        node = node.model_dump(by_alias=True)
    if not isinstance(node, dict):
        out.append(_err("invalid_condition", "Invalid rule condition"))
        return
    if "not" in node and node.get("field") is None and node.get("conditions") is None:
        inner = node.get("not")
        if not isinstance(inner, dict):
            out.append(_err("invalid_condition", "Invalid rule condition"))
            return
        _node_errors(inner, scope_kind, depth + 1, out)
        return
    if isinstance(node.get("conditions"), list):
        if node.get("op", "and") not in ("and", "or"):
            out.append(_err("invalid_group_op", "Condition group operator must be 'and' or 'or'"))
        if not node["conditions"]:
            out.append(_err("empty_group", "Condition group cannot be empty"))
        for child in node["conditions"]:
            _node_errors(child, scope_kind, depth + 1, out)
        return

    field, op, value = node.get("field"), node.get("op"), node.get("value")
    if field not in FIELD_TYPES or op not in ALL_OPS:
        out.append(_err("invalid_condition", "Invalid rule condition", field=field, op=op))
        return
    if field in CARD_ONLY_FIELDS and scope_kind != ACCOUNT_KIND_CARD:
        out.append(_err(
            "field_not_in_scope",
            f"The field '{field}' only exists on credit cards: scope the rule to credit cards",
            field=field,
        ))
    if op not in allowed_ops(field):
        out.append(_err("op_not_for_field", "Invalid rule condition", field=field, op=op))
        return
    if op in VALUELESS_OPS:
        if not _is_blank(value) and value != []:
            out.append(_err("unexpected_value", f"'{op}' takes no value", field=field, op=op))
        return
    if op in LIST_OPS:
        items = value if isinstance(value, list) else [value]
        if not items or any(_is_blank(v) for v in items):
            out.append(_err("missing_value", f"A value is required for '{field}'", field=field))
            return
    elif _is_blank(value) or isinstance(value, list):
        out.append(_err("missing_value", f"A value is required for '{field}'", field=field))
        return
    values = value if isinstance(value, list) else [value]
    allowed_values = ENUM_VALUES.get(field)
    if allowed_values and any(str(v).strip().lower() not in {a.lower() for a in allowed_values} for v in values):
        if field == "status":
            out.append(_err("invalid_condition", "Invalid rule condition", field=field))
        else:
            out.append(_err("invalid_value", f"Invalid value for '{field}'", field=field))
    if field == "status" and op not in ENUM_OPS:
        out.append(_err("invalid_condition", "Invalid rule condition", field=field))
    if op in ("regex", "not_regex"):
        try:
            compile_rule_regex(str(value))
        except ValueError as exc:
            out.append(_err("invalid_regex", str(exc), pattern=str(value)))


def rule_definition_errors(
    scope_kind: str | None,
    conditions_op: str,
    conditions: list,
    actions: list,
) -> list[dict]:
    """Every problem of a rule definition against its scope, without the DB:
    unknown field for the scope, operator/field mismatch, invalid regex,
    missing or unexpected value, action not allowed for the scope.
    Empty list = valid. Category/payee existence is the service's job."""
    errors: list[dict] = []
    if scope_kind is not None and scope_kind not in ACCOUNT_KINDS:
        errors.append(_err("invalid_scope", "Invalid rule scope"))
    if conditions_op not in ("and", "or"):
        errors.append(_err("invalid_condition", "Invalid rule condition"))
    for node in conditions or []:
        _node_errors(node, scope_kind, 1, errors)
    for action in actions or []:
        op = _get(action, "op")
        value = _get(action, "value")
        if op not in ACTION_OPS:
            errors.append(_err("invalid_action", "Invalid rule action", op=op))
            continue
        if op in CARD_ONLY_ACTIONS and scope_kind != ACCOUNT_KIND_CARD:
            errors.append(_err(
                "action_not_in_scope",
                f"The action '{op}' only applies to credit cards: scope the rule to credit cards",
                op=op,
            ))
        if op == "set_card_line_kind" and value not in CARD_LINE_KINDS:
            errors.append(_err("invalid_card_kind", "Invalid card line kind", value=value))
        if op == "strip_installment_marker":
            try:
                compile_rule_regex(str(value or ""))
            except ValueError as exc:
                errors.append(_err("invalid_regex", str(exc), pattern=str(value)))
        if op == "set_description":
            if not isinstance(value, str) or not value.strip():
                errors.append(_err("blank_description", "Description cannot be blank"))
            elif len(value.strip()) > 500:
                errors.append(_err("long_description", "Description cannot exceed 500 characters"))
    return errors


def card_line_kind_for(rules, tx, account_type: str | None):
    """(kind, rule) from the highest-priority matching set_card_line_kind
    rule; ``rules`` is already in priority order and in scope."""
    for rule in rules:
        for action in rule.actions or []:
            if action.get("op") != "set_card_line_kind":
                continue
            if evaluate_conditions(rule.conditions_op, rule.conditions or [], tx, account_type):
                return action.get("value"), rule
    return None, None


def strip_patterns_for(rules, tx, account_type: str | None) -> list[str]:
    """Installment markers to remove from this row's description when
    grouping parcels and naming plans (never from the stored description)."""
    patterns: list[str] = []
    for rule in rules:
        for action in rule.actions or []:
            if action.get("op") != "strip_installment_marker":
                continue
            if evaluate_conditions(rule.conditions_op, rule.conditions or [], tx, account_type):
                patterns.append(str(action.get("value")))
    return patterns


def strip_description(description: str, patterns: list[str]) -> str:
    text = description or ""
    for pattern in patterns:
        try:
            text = re.sub(_strip_accents(pattern), " ", text, flags=re.IGNORECASE)
        except re.error:
            continue
    return re.sub(r"\s{2,}", " ", text).strip()


def merge_notes(existing: str | None, incoming: str | None) -> str | None:
    """Combine two note strings the way `append_notes` combines tags.

    Used when an incoming charge is folded into a row that already has notes:
    the existing text is never dropped, the incoming one is only appended when
    it is not already in there.
    """
    incoming = (incoming or "").strip()
    if not incoming:
        return existing
    existing = (existing or "").strip()
    if not existing:
        return incoming
    if incoming in existing:
        return existing
    return f"{existing} {incoming}"


def apply_rule_actions(
    actions: list[dict],
    tx: "Transaction",
    category_already_set: bool,
    *,
    skip_description: bool = False,
    assignable_category_ids: Collection[uuid.UUID] | None = None,
    rule_id: uuid.UUID | None = None,
) -> bool:
    """Apply actions in-place and return the updated category-set flag.

    Only a category in `assignable_category_ids` is ever written. A rule that
    names any other one keeps its remaining actions and drops the
    categorization, leaving the transaction uncategorized.

    That covers two cases. A hidden category means the user stopped using it,
    so the pickers no longer offer it and neither should a rule. A category
    that is simply gone means the rule went stale: its id lives inside a JSON
    action with no foreign key behind it, and writing it would fail the insert
    and take the whole import or sync batch with it.

    Passing nothing skips the check, for callers with no workspace at hand.
    """
    for action in actions:
        op = action.get("op")
        value = action.get("value")

        if op == "set_category" and not category_already_set:
            try:
                category_id = uuid.UUID(str(value))
            except (ValueError, AttributeError):
                continue
            if assignable_category_ids is not None and category_id not in assignable_category_ids:
                continue
            tx.category_id = category_id
            # Provenance for the sticky classification (RFC §5.3).
            tx.category_source = "rule"
            tx.category_rule_id = rule_id
            category_already_set = True

        elif op == "clear_category" and not category_already_set:
            tx.category_id = None
            tx.category_source = "rule"
            tx.category_rule_id = rule_id
            category_already_set = True

        elif op == "set_description":
            if skip_description:
                continue
            description = str(value or "").strip()
            if not description or description == tx.description:
                continue
            if getattr(tx, "original_description", None) is None:
                tx.original_description = tx.description
            tx.description = description
            tx.description_is_rule_managed = True

        elif op == "set_payee":
            try:
                tx.payee_id = uuid.UUID(str(value))
            except (ValueError, AttributeError):
                pass

        elif op == "append_notes":
            new_tags = str(value or "").strip()
            if not new_tags:
                continue
            existing = tx.notes or ""
            if new_tags not in existing:
                tx.notes = (existing + " " + new_tags).strip() if existing else new_tags

        elif op == "ignore":
            if not tx.is_ignored:
                tx.is_ignored = True
                tx.ignored_reason = "user"

    return category_already_set
