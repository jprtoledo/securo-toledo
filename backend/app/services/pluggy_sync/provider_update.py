"""How a stored row follows the bank (RFC §4.6, INV-2).

Provider-owned fields — the bank's truth — are rewritten from the latest
payload on every sync: date, amount, currency, type, status, installment
metadata, every §4.3 column, raw_data (replaced, not merged) and the bill
link. User-owned fields are never touched: category and its provenance,
the payee the user chose, notes, splits, attachments, the manual bill
override, recurrence/plan decisions, exclude_from_pnl.

Two states freeze the visible fields and only refresh raw_data and the
provider_* columns: the user's sync lock ("Don't update from the bank")
and a user ignore.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.models.credit_card_bill import CreditCardBill
from app.models.transaction import Transaction
from app.providers.base import TransactionData

# Refreshed even on locked/frozen rows: they explain the row, they do not
# change what it shows.
PROVIDER_COLUMNS = (
    ("provider_id", "provider_id"),
    ("provider_code", "provider_code"),
    ("provider_created_at", "provider_created_at"),
    ("provider_updated_at", "provider_updated_at"),
    ("provider_description_raw", "description_raw"),
    ("operation_type", "operation_type"),
    ("provider_category", "pluggy_category"),
    ("provider_category_id", "provider_category_id"),
    ("merchant_name", "merchant_name"),
    ("merchant_cnpj", "merchant_cnpj"),
    ("merchant_mcc", "merchant_mcc"),
    ("card_number", "card_number"),
    ("payment_type", "payment_type"),
    ("transaction_at", "transaction_at"),
    ("provider_bill_external_id", "bill_external_id"),
    ("bill_forecast_month", "bill_forecast_month"),
    ("bill_post_date", "bill_post_date"),
    ("amount_in_account_currency", "amount_in_account_currency"),
    ("balance_after", "balance_after"),
    ("payment_data", "payment_data"),
)

# Visible fields the bank owns.
VISIBLE_COLUMNS = (
    ("date", "date"),
    ("amount", "amount"),
    ("type", "type"),
    ("status", "status"),
    ("installment_number", "installment_number"),
    ("total_installments", "total_installments"),
    ("installment_total_amount", "installment_total_amount"),
    ("installment_purchase_date", "installment_purchase_date"),
)


def set_if_changed(obj: Any, attr: str, value: Any) -> bool:
    """Assign only when the value differs, so an unchanged sync issues no
    UPDATE at all (INV-3). Returns whether it changed."""
    if getattr(obj, attr) == value:
        return False
    setattr(obj, attr, value)
    return True


def _same_instant(a, b) -> bool:
    if a is None or b is None:
        return a is b
    # SQLite hands timestamps back naive; compare on the wall clock then.
    if (a.tzinfo is None) != (b.tzinfo is None):
        return a.replace(tzinfo=None) == b.replace(tzinfo=None)
    return a == b


def apply_provider_columns(tx: Transaction, data: TransactionData) -> bool:
    changed = False
    for column, attr in PROVIDER_COLUMNS:
        value = getattr(data, attr)
        if column in ("provider_created_at", "provider_updated_at", "transaction_at"):
            if _same_instant(getattr(tx, column), value):
                continue
        changed |= set_if_changed(tx, column, value)
    if data.raw_data is not None:
        changed |= set_if_changed(tx, "raw_data", data.raw_data)
    return changed


def is_user_ignored(tx: Transaction) -> bool:
    return bool(tx.is_ignored) and tx.ignored_reason in (None, "user")


def is_frozen(tx: Transaction) -> bool:
    """Visible fields stay as they are: locked by the user, or ignored by
    the user (today's behavior: a hidden row is not revived by sync)."""
    return bool(tx.sync_locked) or is_user_ignored(tx)


def has_manual_description(tx: Transaction) -> bool:
    # Same test as rule_service._has_manual_description (kept local to
    # avoid importing the rule service from the sync helpers).
    return (
        not tx.description_is_rule_managed
        and tx.original_description is not None
        and tx.description != tx.original_description
    )


@dataclass
class UpdateOutcome:
    changed: bool = False
    # Amount, currency, date or the bank conversion moved: FX must be restamped.
    money_changed: bool = False
    # Date or bill link moved: effective date must be re-resolved.
    placement_changed: bool = False
    restored: bool = False


def restore_if_removed(tx: Transaction) -> bool:
    """The row came back from the provider: undo a removal by absence."""
    if tx.provider_removed_at is None:
        return False
    tx.provider_removed_at = None
    if tx.ignored_reason == "provider_removed":
        tx.is_ignored = False
        tx.ignored_reason = None
    return True


def apply_visible_fields(
    tx: Transaction,
    data: TransactionData,
    *,
    currency: str,
    bill: Optional[CreditCardBill],
) -> UpdateOutcome:
    """Rewrite the bank-owned visible fields of an unlocked row."""
    out = UpdateOutcome()
    before_money = (tx.amount, tx.currency, tx.date)
    before_date = tx.date

    for column, attr in VISIBLE_COLUMNS:
        out.changed |= set_if_changed(tx, column, getattr(data, attr))
    out.changed |= set_if_changed(tx, "currency", currency)

    # Description follows the bank only while the user has not renamed it.
    # Rule-managed text is the rules' job; original_description always
    # carries the bank's own text.
    if not has_manual_description(tx) and not tx.description_is_rule_managed:
        out.changed |= set_if_changed(tx, "description", data.description)
    out.changed |= set_if_changed(tx, "original_description", data.description)
    if data.payee and not tx.payee:
        out.changed |= set_if_changed(tx, "payee", data.payee)

    # Bill link, unless the user moved the row to another bill by hand.
    if tx.effective_bill_date is None:
        bill_id = bill.id if bill is not None else None
        if data.bill_external_id is None or bill is not None:
            out.placement_changed |= set_if_changed(tx, "bill_id", bill_id)

    out.money_changed = before_money != (tx.amount, tx.currency, tx.date)
    out.placement_changed |= before_date != tx.date
    out.changed |= out.placement_changed
    return out


def update_existing_row(
    tx: Transaction,
    data: TransactionData,
    *,
    currency: str,
    bill: Optional[CreditCardBill],
) -> UpdateOutcome:
    """Apply one provider payload to a stored row (§4.6). Returns what moved
    so the caller can restamp FX and re-resolve the effective date."""
    conversion_before = tx.amount_in_account_currency
    out = UpdateOutcome()
    out.restored = restore_if_removed(tx)
    out.changed |= out.restored
    out.changed |= apply_provider_columns(tx, data)
    if is_frozen(tx):
        return out
    visible = apply_visible_fields(tx, data, currency=currency, bill=bill)
    out.changed |= visible.changed
    out.placement_changed = visible.placement_changed
    out.money_changed = visible.money_changed or (
        conversion_before != data.amount_in_account_currency
    )
    return out
