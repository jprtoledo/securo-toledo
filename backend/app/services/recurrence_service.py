"""Recurrences v2 in the database (RFC toledo-features §8).

- Recompute step 5 links every live row of an account to the periods of the
  account's recurrences, from scratch (``link_step``).
- Timelines, alerts and the computed forecast (``timeline_for``,
  ``projected_occurrences``) — the forecast replaces materialized
  placeholders for recurrences with ``auto_generate = false``.
- Suggestions and the user's review decisions.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.models.account import Account
from app.models.recurring_suggestion_dismissal import RecurringSuggestionDismissal
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.services import card_statement_service as cards
from app.services.pluggy_sync.provider_update import set_if_changed
from app.services.recompute.billing_cycle import month_for_date_in_cycle
from app.services.recompute.months import add_months, month_of
from app.services.recompute.recurrence_suggestions import SuggestionRow, suggest
from app.services.recompute.recurrences import (  # noqa: F401 — forecast re-exported for the API
    Occurrence,
    RecurrenceConfig,
    RecurrenceRow,
    build_timeline,
    forecast,  # noqa: F401
    link_recurrences,
    timeline_alerts,
)
from app.services.recompute.statements import open_month_for
from app.services.rule_engine import evaluate_node
from app.services.text_similarity import token_overlap

FORECAST_MONTHS = 12
LEGACY_SIMILARITY = 0.6
TRANSFER_EXCEPTIONS = {"FOLHA_PAGAMENTO"}


class RecurrenceDecisionError(ValueError):
    pass


def to_config(rec: RecurringTransaction) -> RecurrenceConfig:
    return RecurrenceConfig(
        id=rec.id,
        direction=rec.type,
        currency=rec.currency,
        amount=Decimal(rec.amount),
        frequency=rec.frequency,
        start_date=rec.start_date,
        day_of_month=rec.day_of_month,
        end_date=rec.end_date,
        amount_mode=rec.amount_mode or "fixed",
        tolerance_pct=Decimal(rec.amount_tolerance_pct or 0),
        day_window=rec.day_window,
        weekend_adjustment=rec.weekend_adjustment or "none",
        billing_weekday=rec.billing_weekday,
        week_count_basis=rec.week_count_basis,
        enabled=bool(rec.is_active),
        skipped=frozenset(date.fromisoformat(p) for p in rec.skipped_periods or []),
    )


def row_direction(tx: Transaction, account: Account) -> Optional[str]:
    """The direction a row can fulfill a recurrence with, or None when it
    can never be recurring (parcels, bill payments, previous balance,
    carried-over credits, own-account transfers — payroll excepted)."""
    if tx.source == "opening_balance":
        return None
    if tx.transfer_pair_id is not None and (tx.operation_type or "") not in TRANSFER_EXCEPTIONS:
        return None
    if (tx.operation_type or "") == "PAGAMENTO_FATURA" and account.type != "credit_card":
        return None
    if account.type == "credit_card":
        if tx.installment_plan_id is not None or (tx.total_installments or 0) > 1:
            return None
        if tx.card_line_kind_reason == "carried_over":
            return None
        if tx.card_line_kind == "purchase":
            return "debit"
        if tx.card_line_kind == "credit":
            return "credit"
        if tx.card_line_kind is None:
            return tx.type
        return None
    return tx.type


def _live(tx: Transaction) -> bool:
    return tx.provider_removed_at is None and tx.ignored_reason != "duplicate"


def _rows(account: Account, rows: list[Transaction]) -> tuple[list[RecurrenceRow], dict]:
    out: list[RecurrenceRow] = []
    by_id: dict = {}
    for tx in rows:
        if not _live(tx):
            continue
        placeholder = tx.recurring_transaction_id if tx.source == "recurring" else None
        direction = tx.type if placeholder else row_direction(tx, account)
        if direction is None:
            continue
        row = RecurrenceRow(
            id=tx.id,
            date=tx.date,
            description=tx.description or "",
            direction=direction,
            currency=tx.currency,
            amount=abs(Decimal(tx.amount)),
            account_amount=abs(cards.account_amount(tx, account)),
            manual_id=tx.recurring_manual_id,
            manual_none=bool(tx.recurring_manual_none),
            manual_excluded=bool(tx.recurring_manual_excluded),
            rejected_ids=tuple(uuid.UUID(str(r)) for r in (tx.recurring_rejected_ids or [])),
            placeholder_for=placeholder,
        )
        out.append(row)
        by_id[tx.id] = tx
    return out, by_id


def _matcher(recs: list[RecurringTransaction], tx_by_id: dict, account_types: dict):
    """``account_types``: account id → type (rows may come from several
    accounts when a recurrence is paid from more than one)."""
    by_id = {r.id: r for r in recs}

    def matches(config: RecurrenceConfig, row: RecurrenceRow) -> bool:
        rec = by_id[config.id]
        tx = tx_by_id[row.id]
        if rec.match_condition:
            return evaluate_node(rec.match_condition, tx, account_types[tx.account_id])
        # Legacy recurrences: the description similarity they always used.
        return any(
            text and token_overlap(rec.description, text) >= LEGACY_SIMILARITY
            for text in (tx.description, tx.original_description)
        )

    return matches


def account_ids_of(rec: RecurringTransaction) -> list[uuid.UUID]:
    """Every account that may pay the recurrence: its own (where the
    forecast lands) first, then the extra ones."""
    extra = [uuid.UUID(str(a)) for a in rec.extra_account_ids or []]
    return [rec.account_id, *(a for a in extra if a != rec.account_id)] if rec.account_id else extra


async def load_recurrences(session: AsyncSession, account_id: uuid.UUID) -> list[RecurringTransaction]:
    """The recurrences whose forecast lands on the account."""
    return list(
        (
            await session.execute(
                select(RecurringTransaction).where(RecurringTransaction.account_id == account_id)
            )
        ).scalars().all()
    )


async def recurrences_paid_from(session: AsyncSession, account: Account) -> list[RecurringTransaction]:
    """The recurrences the account may pay: its own and those listing it as
    an extra account."""
    own = await load_recurrences(session, account.id)
    shared = (
        await session.execute(
            select(RecurringTransaction).where(
                RecurringTransaction.workspace_id == account.workspace_id,
                RecurringTransaction.extra_account_ids.is_not(None),
                RecurringTransaction.account_id != account.id,
            )
        )
    ).scalars().all()
    return [*own, *(r for r in shared if account.id in account_ids_of(r))]


async def _account_rows(session: AsyncSession, account_id: uuid.UUID) -> list[Transaction]:
    return list(
        (await session.execute(select(Transaction).where(Transaction.account_id == account_id))).scalars().all()
    )


async def _linking_group(session: AsyncSession, account: Account, rows: list[Transaction]):
    """The accounts and recurrences that must be linked together with
    ``account``: a recurrence paid from several accounts competes for the
    rows of each of them, so every recurrence those accounts may pay joins
    (transitively). Returns (accounts by id, rows by account id, recurrences)."""
    accounts = {account.id: account}
    rows_by_account = {account.id: rows}
    recs = {r.id: r for r in await recurrences_paid_from(session, account)}
    while True:
        missing = {a for r in recs.values() for a in account_ids_of(r)} - accounts.keys()
        if not missing:
            break
        for account_id in missing:
            other = await session.get(Account, account_id)
            accounts[account_id] = other
            if other is None:
                continue
            rows_by_account[account_id] = await _account_rows(session, account_id)
            for r in await recurrences_paid_from(session, other):
                recs.setdefault(r.id, r)
    return {k: v for k, v in accounts.items() if v is not None}, rows_by_account, list(recs.values())


@dataclass
class MonthContext:
    month_for_date: object
    open_month: date


async def month_context(session: AsyncSession, account: Account, rows=None, *, today: date) -> MonthContext:
    """Where a value lands and the first month still open: the bill on
    cards (via the cycle), the calendar month on bank accounts."""
    if account.type != "credit_card":
        return MonthContext(month_of, month_of(today))
    if rows is None:
        rows = list(
            (await session.execute(select(Transaction).where(Transaction.account_id == account.id))).scalars().all()
        )
    bills, _, _ = cards.known_bills(await cards.load_bills(session, account.id), rows)
    cycle = cards.cycle_for(account, bills)

    def month_for_date(d: date) -> date:
        return month_for_date_in_cycle(d, bills, cycle) or month_of(d)

    return MonthContext(month_for_date, open_month_for(today, {b.month: b for b in bills}, cycle))


def fx_rates(account: Account, rows: list[Transaction], today: date) -> dict:
    """Account-currency units per foreign unit, median of the last 90 days."""
    by_currency: dict = {}
    for tx in rows:
        if tx.currency == account.currency or tx.date < today - timedelta(days=90) or not tx.amount:
            continue
        account_amount = cards.account_amount(tx, account)
        if account_amount == Decimal(tx.amount):
            continue
        by_currency.setdefault(tx.currency, []).append(account_amount / Decimal(tx.amount))
    return {c: Decimal(median(v)) for c, v in by_currency.items()}


async def link_step(
    session: AsyncSession, account: Account, rows: list[Transaction], changes
) -> dict:
    """Recompute step 5: link rows to recurrence periods (from scratch).

    A recurrence paid from several accounts is linked over the rows of all
    of them (see ``_linking_group``), and those accounts' rows are
    updated too."""
    accounts, rows_by_account, recs = await _linking_group(session, account, rows)
    rec_rows: list[RecurrenceRow] = []
    tx_by_id: dict = {}
    for account_id, acc in accounts.items():
        acc_rows, acc_by_id = _rows(acc, rows_by_account[account_id])
        rec_rows.extend(acc_rows)
        tx_by_id.update(acc_by_id)
    today = app_today()
    # Through the end of next month: a charge posted ahead still finds its period.
    through = add_months(month_of(today), 2) - timedelta(days=1)
    links = (
        link_recurrences(
            [to_config(r) for r in recs], rec_rows,
            _matcher(recs, tx_by_id, {a.id: a.type for a in accounts.values()}),
            through, account.currency,
        )
        if recs else {}
    )
    all_rows = [tx for account_id in accounts for tx in rows_by_account[account_id]]
    for tx in all_rows:
        a = links.get(tx.id)
        if a is not None:
            changes.set(tx, "recurring_transaction_id", a.recurrence_id)
            changes.set(tx, "recurring_period", a.period)
            changes.set(tx, "recurring_link", a.link)
            changes.set(tx, "recurring_issue", a.issue)
            changes.set(tx, "recurring_companion", a.companion)
        else:
            # Unlinked — including a link to a recurrence this account no
            # longer pays (moved or dropped from its accounts).
            if tx.source == "recurring" and tx.recurring_transaction_id is not None:
                continue
            changes.set(tx, "recurring_transaction_id", None)
            changes.set(tx, "recurring_period", None)
            changes.set(tx, "recurring_link", None)
            changes.set(tx, "recurring_issue", None)
            changes.set(tx, "recurring_companion", False)

    # next_occurrence follows the first slot still to come, for computed
    # recurrences; auto-generating ones keep the pointer generate_pending owns.
    if recs:
        linked = _occurrences_by_recurrence(all_rows, accounts)
        contexts: dict = {}
        for rec in recs:
            main = accounts.get(rec.account_id)
            if rec.auto_generate or not rec.is_active or main is None:
                continue
            main_rows = rows_by_account[main.id]
            if main.id not in contexts:
                contexts[main.id] = await month_context(session, main, main_rows, today=today)
            timeline = _timeline(
                rec, linked.get(rec.id, []), contexts[main.id], main, today, fx_rates(main, main_rows, today),
            )
            upcoming = forecast(timeline)
            if upcoming:
                set_if_changed(rec, "next_occurrence", upcoming[0].period)
    conflicts = sum(1 for a in links.values() if a.link == "conflict")
    return {"recurrences": len(recs), "linked": len(links), "recurring_conflicts": conflicts}


def _occurrences_by_recurrence(rows: list[Transaction], accounts: dict) -> dict:
    """``accounts``: account id → Account, for every account ``rows`` come from."""
    out: dict = {}
    for tx in rows:
        if tx.recurring_transaction_id is None or tx.recurring_period is None or not _live(tx):
            continue
        account = accounts[tx.account_id]
        out.setdefault(tx.recurring_transaction_id, []).append(Occurrence(
            tx_id=tx.id,
            period=tx.recurring_period,
            date=tx.date,
            link=tx.recurring_link or "auto",
            companion=bool(tx.recurring_companion),
            amount=abs(Decimal(tx.amount)),
            account_amount=abs(cards.account_amount(tx, account)),
            excluded=bool(tx.recurring_manual_excluded),
            account_id=tx.account_id,
        ))
    return out


def _timeline(rec, linked, ctx: MonthContext, account, today, rates):
    return build_timeline(
        to_config(rec),
        linked,
        today=today,
        through=add_months(ctx.open_month, FORECAST_MONTHS),
        month_for_date=ctx.month_for_date,
        open_month=ctx.open_month,
        account_currency=account.currency,
        fx_rate=rates.get(rec.currency),
    )


@dataclass
class RecurrenceView:
    recurrence: RecurringTransaction
    timeline: list
    alerts: dict


async def views_for_account(
    session: AsyncSession, account: Account, *, today: Optional[date] = None
) -> list[RecurrenceView]:
    today = today or app_today()
    recs = await load_recurrences(session, account.id)
    if not recs:
        return []
    rows = await _account_rows(session, account.id)
    ctx = await month_context(session, account, rows, today=today)
    accounts = {account.id: account}
    paid_elsewhere: list[Transaction] = []
    shared = [r.id for r in recs if r.extra_account_ids]
    if shared:
        # Periods a recurrence's other accounts paid.
        paid_elsewhere = list((await session.execute(
            select(Transaction).where(
                Transaction.recurring_transaction_id.in_(shared),
                Transaction.account_id != account.id,
            )
        )).scalars().all())
        for tx in paid_elsewhere:
            if tx.account_id not in accounts:
                accounts[tx.account_id] = await session.get(Account, tx.account_id)
    linked = _occurrences_by_recurrence([*rows, *paid_elsewhere], accounts)
    rates = fx_rates(account, rows, today)
    out = []
    for rec in recs:
        timeline = _timeline(rec, linked.get(rec.id, []), ctx, account, today, rates)
        out.append(RecurrenceView(rec, timeline, timeline_alerts(timeline)))
    return out


async def timeline_for(session: AsyncSession, rec: RecurringTransaction, *, today=None) -> RecurrenceView:
    account = await session.get(Account, rec.account_id)
    views = await views_for_account(session, account, today=today)
    return next(v for v in views if v.recurrence.id == rec.id)


async def projected_occurrences(
    session: AsyncSession,
    rec: RecurringTransaction,
    range_start: date,
    range_end: date,
    *,
    cache: Optional[dict] = None,
) -> list[tuple[date, Decimal]]:
    """Forecast of a computed recurrence in [range_start, range_end): its
    expected and late slots (RFC §8.5), skipped ones aside, as (expected date, amount in the
    recurrence currency). Cached per account within one request."""
    if rec.account_id is None:
        return []
    cache = cache if cache is not None else {}
    key = ("views", rec.account_id)
    if key not in cache:
        account = await session.get(Account, rec.account_id)
        cache[key] = {v.recurrence.id: v for v in await views_for_account(session, account)} if account else {}
    view = cache[key].get(rec.id)
    if view is None:
        return []
    return [
        (slot.expected_date, slot.expected_amount)
        for slot in forecast(view.timeline)
        if range_start <= slot.expected_date < range_end
    ]


async def card_recurring_projections(session: AsyncSession, account: Account, *, today: date):
    """Expected card charges/credits per bill month (statements §6.4)."""
    from app.services.recompute.statements import Projection

    out = []
    for view in await views_for_account(session, account, today=today):
        if view.recurrence.auto_generate:
            continue
        sign = Decimal(1) if view.recurrence.type == "debit" else Decimal(-1)
        for slot in forecast(view.timeline):
            if slot.amount is not None:
                rec = view.recurrence
                out.append(Projection(slot.month, slot.amount * sign, "recurrence", {
                    "description": rec.description,
                    "ref_id": str(rec.id),
                    "category_id": str(rec.category_id) if rec.category_id else None,
                    "date": slot.expected_date,
                }))
    return out


# --- suggestions ------------------------------------------------------------------------


async def suggestions_for(session: AsyncSession, account: Account, *, today: Optional[date] = None) -> list:
    today = today or app_today()
    rows = list(
        (
            await session.execute(
                select(Transaction).where(
                    Transaction.account_id == account.id,
                    Transaction.date >= today - timedelta(days=400),
                )
            )
        ).scalars().all()
    )
    candidates = []
    for tx in rows:
        if not _live(tx) or tx.recurring_transaction_id is not None or tx.recurring_manual_none:
            continue
        direction = row_direction(tx, account)
        if direction is None:
            continue
        candidates.append(SuggestionRow(tx.id, tx.date, tx.description or "", direction, tx.currency, abs(Decimal(tx.amount))))
    dismissed = {
        row[0]
        for row in (
            await session.execute(
                select(RecurringSuggestionDismissal.key).where(RecurringSuggestionDismissal.account_id == account.id)
            )
        ).all()
    }
    return suggest(candidates, today, dismissed)


async def dismiss_suggestion(session: AsyncSession, account: Account, key: str) -> None:
    exists = (
        await session.execute(
            select(RecurringSuggestionDismissal.id).where(
                RecurringSuggestionDismissal.account_id == account.id,
                RecurringSuggestionDismissal.key == key,
            )
        )
    ).first()
    if exists is None:
        session.add(RecurringSuggestionDismissal(workspace_id=account.workspace_id, account_id=account.id, key=key))


def draft_from_transaction(tx: Transaction, account: Account) -> dict:
    """"Make recurring" (RFC §8.6): a recurrence prefilled from one row.
    Refused for parcels, bill payments and previous-balance lines (AC-6.15)."""
    from app.services.rule_engine import description_key

    if (
        row_direction(tx, account) is None
        or tx.installment_plan_id is not None
        or (tx.total_installments or 0) > 1
    ):
        raise RecurrenceDecisionError("This transaction cannot be a recurring charge")
    key = description_key(tx.description)
    return {
        "description": tx.description,
        "amount": abs(Decimal(tx.amount)),
        "currency": tx.currency,
        "type": row_direction(tx, account),
        "frequency": "monthly",
        "day_of_month": tx.date.day,
        "start_date": tx.date,
        "account_id": tx.account_id,
        "category_id": tx.category_id,
        "amount_mode": "fixed",
        "amount_tolerance_pct": Decimal("0"),
        "day_window": None,
        "auto_generate": False,
        "match_condition": (
            {"op": "and", "conditions": [{"field": "description_key", "op": "starts_with", "value": key}]}
            if len(key) >= 3 else None
        ),
    }


# --- review decisions (RFC §8.4) -------------------------------------------------------


def _rejected(tx: Transaction) -> list:
    return [uuid.UUID(str(r)) for r in (tx.recurring_rejected_ids or [])]


def link_manually(tx: Transaction, recurring_id) -> None:
    """Link to a recurrence (``recurring_id``), mark "not a recurring charge"
    (``"none"``) or undo every decision (``None``)."""
    if recurring_id is None:
        tx.recurring_manual_id = None
        tx.recurring_manual_none = False
        tx.recurring_manual_excluded = False
        return
    if recurring_id == "none":
        tx.recurring_manual_id = None
        tx.recurring_manual_none = True
        tx.recurring_manual_excluded = False
        return
    rid = uuid.UUID(str(recurring_id))
    tx.recurring_manual_id = rid
    tx.recurring_manual_none = False
    tx.recurring_manual_excluded = False
    # Linking by hand lifts a rejection of that recurrence.
    tx.recurring_rejected_ids = [r for r in _rejected(tx) if r != rid] or None


def review(tx: Transaction, decision: str) -> None:
    """accept: stays linked but out of the expected amount; reject: never
    auto-linked to that recurrence again."""
    rec_id = tx.recurring_transaction_id or tx.recurring_manual_id
    if rec_id is None:
        raise RecurrenceDecisionError("This transaction is not linked to a recurring charge")
    if decision == "accept":
        tx.recurring_manual_id = rec_id
        tx.recurring_manual_excluded = True
        tx.recurring_manual_none = False
    elif decision == "reject":
        rejected = _rejected(tx)
        if rec_id not in rejected:
            rejected.append(rec_id)
        tx.recurring_rejected_ids = rejected
        if tx.recurring_manual_id == rec_id:
            tx.recurring_manual_id = None
            tx.recurring_manual_excluded = False
    else:
        raise RecurrenceDecisionError("Unknown decision")


def confirm(tx: Transaction) -> None:
    """A conflict ("amount changed") the user confirms: the new amount
    becomes the reference."""
    rec_id = tx.recurring_transaction_id or tx.recurring_manual_id
    if rec_id is None:
        raise RecurrenceDecisionError("This transaction is not linked to a recurring charge")
    tx.recurring_manual_id = rec_id
    tx.recurring_manual_excluded = False
    tx.recurring_manual_none = False
