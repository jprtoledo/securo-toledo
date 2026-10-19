"""Credit-card context and statement view (RFC toledo-features §6).

Turns the stored bills and rows of a card into the plain inputs of the pure
algorithms in ``app.services.recompute`` (billing cycle, reference month,
statements) — shared by recompute, which writes effective dates, and by the
statements endpoint, which only reads.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.models.account import Account
from app.models.credit_card_bill import CreditCardBill
from app.models.transaction import Transaction
from app.services.recompute.billing_cycle import (
    CycleDays,
    KnownBill,
    derive_closing_dates,
    infer_cycle_days,
)
from app.services.recompute.months import month_of
from app.services.recompute.statements import (
    Projection,
    StatementRow,
    build_statements,
    open_month_for,
)

SYNTHETIC_PREFIX = "bill_charge:"


def account_amount(tx: Transaction, account: Account) -> Decimal:
    """The row's amount in the card's currency (international purchases
    carry the bank's conversion)."""
    if tx.currency == account.currency or account.currency is None:
        return Decimal(tx.amount)
    if tx.amount_in_account_currency is not None:
        return Decimal(tx.amount_in_account_currency)
    if tx.amount_primary is not None:
        return Decimal(tx.amount_primary)
    return Decimal(tx.amount)


def signed_account_amount(tx: Transaction, account: Account) -> Decimal:
    amount = account_amount(tx, account)
    return amount if tx.type == "credit" else -amount


def known_bills(bill_models: list[CreditCardBill], rows: list[Transaction]):
    """KnownBills (closing dates derived where the provider sent none) plus
    lookups by external id and by id."""
    bills = [
        KnownBill(
            id=b.id,
            external_id=b.external_id,
            due_date=b.due_date,
            closing_date=b.closing_date,
            total_amount=b.total_amount,
            minimum_payment=b.minimum_payment,
            finance_charges=b.finance_charges_total,
            payments=list(b.payments or []),
        )
        for b in bill_models
    ]
    by_ext = {b.external_id: b for b in bills}
    by_id = {b.id: b for b in bills}
    dates_by_bill: dict = {}
    for tx in rows:
        if tx.type != "debit" or (tx.external_id or "").startswith(SYNTHETIC_PREFIX):
            continue
        bill = bill_for_row(tx, by_ext, by_id)
        if bill is not None:
            dates_by_bill.setdefault(bill.id, []).append(tx.date)
    derive_closing_dates(bills, dates_by_bill)
    return bills, by_ext, by_id


def bill_for_row(tx: Transaction, by_ext: dict, by_id: dict) -> Optional[KnownBill]:
    if tx.provider_bill_external_id:
        bill = by_ext.get(tx.provider_bill_external_id)
        if bill is not None:
            return bill
    if tx.bill_id is not None:
        return by_id.get(tx.bill_id)
    return None


def cycle_for(account: Account, bills: list[KnownBill]) -> Optional[CycleDays]:
    """The user's days when they typed them; else derived from the bills /
    the provider's balance dates; else whatever days the account has."""
    typed = (
        CycleDays(account.statement_close_day, account.payment_due_day)
        if account.statement_close_day and account.payment_due_day
        else None
    )
    if account.cycle_days_manual and typed:
        return typed
    return infer_cycle_days(bills, account.balance_close_date, account.balance_due_date) or typed


async def load_bills(session: AsyncSession, account_id: uuid.UUID) -> list[CreditCardBill]:
    return list(
        (
            await session.execute(
                select(CreditCardBill).where(CreditCardBill.account_id == account_id)
            )
        ).scalars().all()
    )


def statement_rows(account: Account, rows: list[Transaction]) -> list[StatementRow]:
    """Live card rows as statement lines (ignored rows — duplicates, removed
    by the bank, ignored by the user — never count)."""
    out = []
    for tx in rows:
        if tx.is_ignored or tx.source == "opening_balance":
            continue
        kind = tx.card_line_kind or ("credit" if tx.type == "credit" else "purchase")
        out.append(
            StatementRow(
                id=tx.id,
                date=tx.date,
                month=month_of(tx.effective_date or tx.date),
                signed_amount=signed_account_amount(tx, account),
                kind=kind,
                type=tx.type,
                operation_type=tx.operation_type,
            )
        )
    return out


async def get_statements(
    session: AsyncSession,
    account: Account,
    *,
    today: Optional[date] = None,
    projected: Optional[list[Projection]] = None,
) -> dict:
    today = today or app_today()
    rows = list(
        (
            await session.execute(select(Transaction).where(Transaction.account_id == account.id))
        ).scalars().all()
    )
    bills, _, _ = known_bills(await load_bills(session, account.id), rows)
    cycle = cycle_for(account, bills)
    if projected is None:
        from app.services import projection_service

        projected = await projection_service.card_projections(session, account, today=today)
    statements = build_statements(
        bills=bills, rows=statement_rows(account, rows), cycle=cycle, today=today, projected=projected,
    )
    open_month = open_month_for(today, {b.month: b for b in bills}, cycle)
    provider = None
    if account.connection_id:
        from app.models.bank_connection import BankConnection

        provider = await session.scalar(
            select(BankConnection.provider).where(BankConnection.id == account.connection_id)
        )
    return {
        "account_id": account.id,
        # Pluggy cards are bucketed by the reference month (effective dates
        # written by recompute): the account page reads its months from here.
        "reference_months": provider == "pluggy",
        "today": today,
        "open_month": open_month,
        "closing_day": cycle.closing_day if cycle else None,
        "due_day": cycle.due_day if cycle else None,
        "cycle_days_manual": bool(account.cycle_days_manual),
        "resolution_stats": account.resolution_stats,
        "statements": [s.as_dict() for s in statements],
    }
