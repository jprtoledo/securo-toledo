"""Per-account recompute (RFC toledo-features §5.1).

Derives everything that is *computed* from provider data plus the user's
decisions, for one account, from scratch:

    1. effective date / reference month + calibration   (Phase 4)
    2. duplicates (non-destructive)                     (Phase 2)
    3. card line kind (rules + defaults)                (Phase 3/4)
    4. installment plans + validation                   (Phase 5)
    5. recurrence links                                 (Phase 6)
    6. rule-driven classification (sticky semantics)    (Phase 3)

Idempotent (INV-3): a second run with no new input writes nothing. It never
overwrites a user decision (INV-2); it only re-derives computed columns.

Writes: the rows are loaded once as ORM objects and every step assigns
through ``set_if_changed``, so the unit of work flushes only the rows and
columns that actually moved (SQLAlchemy batches those UPDATEs with
executemany). Implementer's choice over a hand-written ``UPDATE … FROM
(VALUES …)``: it keeps the session's identity map coherent with the sync
that usually runs right before, and works on every dialect the test suite
uses. The pure algorithms live in ``app.services.recompute``.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.rule import Rule
from app.models.transaction import Transaction
from app.services import admin_service, provider_category_service
from app.services.category_service import get_assignable_category_ids
from app.services.pluggy_sync.provider_update import is_user_ignored, set_if_changed
from app.services.recompute.card_kind import default_card_kind
from app.services.recompute.classification import Classification, classify
from app.services.recompute.duplicates import DedupRow, find_pending_duplicates
from app.services.recompute.rule_set import RuleSet
from app.services import card_statement_service as cards
from app.services import installment_plan_service, recurrence_service
from app.services.recompute.reference import CardContext, ResolvableRow, calibrate, resolve
from app.services.recompute.statements import find_carried_over_credits, find_payment_echoes

logger = logging.getLogger(__name__)


@dataclass
class RecomputeStats:
    account_id: uuid.UUID
    rows: int = 0
    changed: int = 0
    duplicates: int = 0
    classified: int = 0
    changed_ids: set = field(default_factory=set)
    # Rows whose category or card line kind moved: what a rule change did.
    rule_effect_ids: set = field(default_factory=set)
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "account_id": str(self.account_id),
            "rows": self.rows,
            "changed": self.changed,
            "duplicates": self.duplicates,
            "classified": self.classified,
            **self.extra,
        }


class _Changes:
    """Tracks which rows a recompute touched (for stats and idempotency)."""

    def __init__(self) -> None:
        self.ids: set = set()

    def set(self, row, attr: str, value) -> None:
        if set_if_changed(row, attr, value):
            self.ids.add(row.id)


def signed_amount(tx: Transaction) -> Decimal:
    amount = Decimal(tx.amount)
    return amount if tx.type == "credit" else -amount


def _is_live(tx: Transaction) -> bool:
    return tx.provider_removed_at is None


async def _load(session: AsyncSession, account_id: uuid.UUID):
    account = await session.get(Account, account_id)
    if account is None:
        return None, None, []
    provider = None
    if account.connection_id is not None:
        provider = await session.scalar(
            select(BankConnection.provider).where(BankConnection.id == account.connection_id)
        )
    rows = list(
        (
            await session.execute(
                select(Transaction)
                .where(Transaction.account_id == account_id)
                .order_by(Transaction.date, Transaction.id)
                # Always the stored state (the caller flushed first): a row
                # another session changed must not be judged on stale values.
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )
    return account, provider, rows


async def load_rule_set(session: AsyncSession, account: Account) -> RuleSet:
    rules = (
        await session.execute(
            select(Rule).where(Rule.workspace_id == account.workspace_id, Rule.is_active == True)  # noqa: E712
        )
    ).scalars().all()
    assignable = await get_assignable_category_ids(session, account.workspace_id)
    return RuleSet.compile(
        rules,
        account_id=account.id,
        account_type=account.type,
        assignable_category_ids=assignable,
    )


def _card_kinds(account: Account, rule_set: RuleSet, rows: list[Transaction]) -> dict:
    """Statement role of each credit-card row: a set_card_line_kind rule,
    else the default by type and operation type (RFC §5.3/§6.5).
    Returns id → (kind, source, rule_id, reason)."""
    is_card = account.type == "credit_card"
    kinds = {}
    for tx in rows:
        if not is_card or tx.source == "opening_balance":
            kinds[tx.id] = (None, None, None, None)
            continue
        kind, rule_id = rule_set.card_kind(tx)
        if kind is not None:
            kinds[tx.id] = (kind, "rule", rule_id, None)
        else:
            kinds[tx.id] = (default_card_kind(tx.type, tx.operation_type), "default", None, None)
    return kinds


def _step_effective_dates(
    account: Account, rows: list[Transaction], bills, by_ext, by_id, cycle, changes: _Changes
) -> dict | None:
    """Reference month of every row (§6.2) and, for cards, the calibration
    of the open-bill fallbacks. Returns the calibration stats (cards)."""
    is_card = account.type == "credit_card"
    resolvable: dict = {}
    for tx in rows:
        bill = cards.bill_for_row(tx, by_ext, by_id) if is_card else None
        resolvable[tx.id] = ResolvableRow(
            date=tx.date,
            effective_bill_date=tx.effective_bill_date,
            bill=bill,
            bill_forecast_month=tx.bill_forecast_month,
            bill_post_date=tx.bill_post_date,
        )
    stats = None
    ctx = None
    if is_card:
        ctx = CardContext(bills=bills, cycle=cycle)
        truth = [
            r for tx in rows
            if (r := resolvable[tx.id]).bill is not None and not (tx.is_ignored and tx.ignored_reason == "duplicate")
        ]
        stats = calibrate(truth, ctx)
        ctx = CardContext(
            bills=bills,
            cycle=cycle,
            forecast_offset_months=stats["forecast_offset_months"],
            fallback_order=tuple(stats["fallback_order"]),
        )
    for tx in rows:
        row = resolvable[tx.id]
        result = resolve(row, ctx)
        changes.set(tx, "effective_date", result.effective_date)
        changes.set(tx, "effective_date_source", result.source)
        # Keep the bill link in step with the bill that placed the row.
        if row.bill is not None and tx.effective_bill_date is None:
            changes.set(tx, "bill_id", row.bill.id)
    return stats


def _step_card_kind(
    account: Account, rule_set: RuleSet, rows: list[Transaction], changes: _Changes
) -> None:
    """Card line kinds, with CAIXA's carried-over credits counted as
    previous balance (§6.5): written once, fully resolved."""
    kinds = _card_kinds(account, rule_set, rows)
    if account.type == "credit_card":
        probe = [
            cards.StatementRow(
                id=tx.id,
                date=tx.date,
                month=tx.effective_date.replace(day=1),
                signed_amount=cards.signed_account_amount(tx, account),
                kind=kinds[tx.id][0],
                type=tx.type,
                operation_type=tx.operation_type,
            )
            for tx in rows
            if kinds[tx.id][0] is not None and not tx.is_ignored
        ]
        ledger_months = {r.month for r in probe if r.kind == "previous_balance"}
        for tx_id in find_carried_over_credits(probe, ledger_months):
            if kinds[tx_id][1] == "default":
                kinds[tx_id] = ("previous_balance", "default", None, "carried_over")
    for tx in rows:
        kind, source, rule_id, reason = kinds[tx.id]
        changes.set(tx, "card_line_kind", kind)
        changes.set(tx, "card_line_kind_source", source)
        changes.set(tx, "card_line_kind_rule_id", rule_id)
        changes.set(tx, "card_line_kind_reason", reason)


def _payment_echoes(account: Account, rows: list[Transaction]) -> dict:
    """Pluggy's synthesized payment receipts on ledger statements (§6.5):
    echo id → the bank's own payment line it duplicates (or None)."""
    if account.type != "credit_card":
        return {}
    live = [
        tx for tx in rows
        if tx.card_line_kind is not None and tx.provider_removed_at is None
        and not (tx.is_ignored and tx.ignored_reason in (None, "user"))
    ]
    probe = [
        cards.StatementRow(
            id=tx.id, date=tx.date, month=tx.effective_date.replace(day=1),
            signed_amount=cards.signed_account_amount(tx, account), kind=tx.card_line_kind,
            type=tx.type, operation_type=tx.operation_type,
        )
        for tx in live
    ]
    echo_ids = find_payment_echoes(probe)
    by_id = {r.id: r for r in probe}
    out = {}
    for echo_id in echo_ids:
        echo = by_id[echo_id]
        twin = next(
            (r.id for r in probe
             if r.id not in echo_ids and r.kind == "payment" and r.month == echo.month
             and abs(r.signed_amount) == abs(echo.signed_amount)),
            None,
        )
        out[echo_id] = twin
    return out


def _step_classification(
    rule_set: RuleSet, rows: list[Transaction], changes: _Changes, provider_category,
    plan_categories: dict | None = None,
) -> int:
    """Sticky rule-driven classification (RFC §5.3); returns rows changed.
    A parcel without a choice of its own takes the category the user chose
    for its purchase (the plan's, §7.2 — AC-5.10)."""
    changed = 0
    plan_categories = plan_categories or {}
    for tx in rows:
        if tx.source == "opening_balance":
            continue
        current = Classification(tx.category_id, tx.category_source, tx.category_rule_id)
        plan_category = plan_categories.get(tx.installment_plan_id) if tx.installment_plan_id else None
        if (
            plan_category is not None
            and current.source != "manual"
            and plan_category in rule_set.assignable_category_ids
        ):
            after = Classification(plan_category, "manual", None)
        else:
            after = classify(current, rule_set.category_decision(tx), provider_category(tx))
        if after == current:
            continue
        changes.set(tx, "category_id", after.category_id)
        changes.set(tx, "category_source", after.source)
        changes.set(tx, "category_rule_id", after.rule_id)
        changed += 1
    return changed


def _step_duplicates(
    account: Account, provider: str | None, rows: list[Transaction], changes: _Changes,
    echoes: dict,
) -> int:
    """Flag pending copies of posted card rows and payment echoes; unflag
    rows that stopped qualifying. A user ignore is never overridden; a
    user's "not a duplicate" (duplicate_override) is never re-flagged."""
    applies = provider == "pluggy" and account.type == "credit_card"
    found: dict = {}
    not_ledger = set(echoes)
    if applies:
        found = find_pending_duplicates(
            [
                DedupRow(
                    id=tx.id,
                    date=tx.date,
                    signed_amount=signed_amount(tx),
                    currency=tx.currency,
                    status=tx.status,
                    has_bill_link=tx.provider_bill_external_id is not None,
                    installment_number=tx.installment_number,
                    total_installments=tx.total_installments,
                    purchase_date=tx.installment_purchase_date,
                    transaction_at=tx.transaction_at,
                    removed=not _is_live(tx),
                    override=bool(tx.duplicate_override),
                )
                for tx in rows
                if tx.source == "sync"
            ],
            not_ledger,
        )
    for echo_id, twin in echoes.items():
        found.setdefault(echo_id, twin)
    overridden = {tx.id for tx in rows if tx.duplicate_override}
    for tx in rows:
        target = found.get(tx.id)
        flagged = tx.id in found and tx.id not in overridden
        if flagged:
            changes.set(tx, "duplicate_of_id", target)
            if not is_user_ignored(tx):
                changes.set(tx, "is_ignored", True)
                changes.set(tx, "ignored_reason", "duplicate")
        else:
            if tx.duplicate_of_id is not None:
                changes.set(tx, "duplicate_of_id", None)
            if tx.ignored_reason == "duplicate":
                changes.set(tx, "is_ignored", False)
                changes.set(tx, "ignored_reason", None)
    return len(found)


async def recompute_account(session: AsyncSession, account_id: uuid.UUID) -> RecomputeStats:
    """Recompute one account. Flushes; the caller owns the transaction."""
    await session.flush()
    stats = RecomputeStats(account_id=account_id)
    account, provider, rows = await _load(session, account_id)
    if account is None:
        return stats
    stats.rows = len(rows)
    changes = _Changes()

    rule_set = await load_rule_set(session, account)
    provider_category = await provider_category_service.load_resolver(
        session,
        account.workspace_id,
        enabled=await admin_service.use_provider_categories(session),
    )

    bills, by_ext, by_id = [], {}, {}
    cycle = None
    if account.type == "credit_card":
        bills, by_ext, by_id = cards.known_bills(await cards.load_bills(session, account.id), rows)
        cycle = cards.cycle_for(account, bills)

    calibration = _step_effective_dates(account, rows, bills, by_ext, by_id, cycle, changes)
    kinds_before = {tx.id: (tx.card_line_kind, tx.card_line_kind_rule_id) for tx in rows}
    categories_before = {tx.id: (tx.category_id, tx.category_rule_id) for tx in rows}
    _step_card_kind(account, rule_set, rows, changes)
    echoes = _payment_echoes(account, rows)
    stats.duplicates = _step_duplicates(account, provider, rows, changes, echoes)
    plan_sync = await installment_plan_service.sync_plans(session, account, rows, rule_set, changes)
    stats.extra["plans"] = len(plan_sync.plans)
    stats.extra["installment_conflicts"] = plan_sync.conflicts
    stats.extra.update(await recurrence_service.link_step(session, account, rows, changes))
    stats.classified = _step_classification(
        rule_set, rows, changes, provider_category,
        {p.id: p.category_id for p in plan_sync.plans.values() if p.category_id},
    )

    if account.type == "credit_card":
        if not account.cycle_days_manual and cycle is not None:
            set_if_changed(account, "statement_close_day", cycle.closing_day)
            set_if_changed(account, "payment_due_day", cycle.due_day)
        if calibration is not None:
            set_if_changed(account, "forecast_offset_months", calibration["forecast_offset_months"])
            set_if_changed(account, "resolution_stats", calibration)
    stats.changed_ids = set(changes.ids)
    stats.rule_effect_ids = {
        tx.id for tx in rows
        if kinds_before[tx.id] != (tx.card_line_kind, tx.card_line_kind_rule_id)
        or categories_before[tx.id] != (tx.category_id, tx.category_rule_id)
    }

    stats.changed = len(changes.ids)
    await session.flush()
    if stats.changed:
        logger.info("Recompute of account %s: %s", account_id, stats.as_dict())
    return stats


async def recompute_accounts(
    session: AsyncSession, account_ids: Iterable[uuid.UUID]
) -> list[RecomputeStats]:
    return [await recompute_account(session, account_id) for account_id in account_ids]
