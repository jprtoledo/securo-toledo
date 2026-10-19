"""What a Pluggy card owes on a date, for net worth (Reports follow-up to RFC
toledo-features §6).

Net worth counts a card by its bills: closed bills not paid yet plus what is
already on the open bill. A parcel still to come is owed only once its bill
closes — unlike the bank's used limit, which reserves every parcel of an
installment purchase up front.

Past dates walk back from today's bill debt over the card's purchases,
credits and payments; the previous balance a ledger statement repeats and the
rows the user ignored are not events. Future dates come from the statements:
a bill counts from its closing until its due date (the cash flow pays it
then), the open one with what is on it plus the recurring charges expected by
then. Every recurrence still to come counts unless the user skipped it; there
is no historical estimate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.transaction import Transaction
from app.services.card_statement_service import signed_account_amount
from app.services.recompute.months import add_months


# --- What the card's bills owe on a date (the "bills only" net worth view) ------


@dataclass
class _Cycle:
    opens: date  # previous closing
    closing: date
    due: date
    known_total: Decimal  # posted + parcels + expected recurring (0 for a synthetic cycle)
    real: Decimal  # already on the bill today (open/closed statements)
    recurring: list[tuple[date, Decimal]] = field(default_factory=list)
    unpaid: Decimal = Decimal(0)  # closed bills: what is still owed today
    state: str = "future"


@dataclass
class CardBillDebt:
    """What a card's bills owe on a date: closed bills not paid yet plus what
    is already on the open bill. Parcels still to come count only once their
    bill closes (unlike the bank's used limit, which reserves them all)."""

    today: date
    debt_today: Decimal
    rows: list[tuple[date, Decimal]] = field(default_factory=list)  # signed, account currency
    cycles: list[_Cycle] = field(default_factory=list)

    def at(self, d: date) -> float:
        if d <= self.today:
            debt = self.debt_today
            for when, signed in self.rows:
                if d < when <= self.today:
                    debt += signed  # a purchase after d was not owed yet; a payment was
            return float(debt)
        total = Decimal(0)
        for c in self.cycles:
            if c.state in ("closed", "closed_estimated"):
                if c.unpaid > 0 and d < max(c.due, self.today + timedelta(days=1)):
                    total += c.unpaid
                continue
            if d >= c.due:
                continue  # paid on its due date (the cash flow takes it then)
            if d >= c.closing:
                total += c.known_total
            elif d > c.opens:
                total += c.real + sum((amt for when, amt in c.recurring if when <= d), Decimal(0))
        return float(total)


async def load_card_bill_debt(
    session: AsyncSession, account: Account, *, today: date,
) -> CardBillDebt:
    from app.services.card_statement_service import get_statements

    data = await get_statements(session, account, today=today)
    statements = [s for s in data["statements"] if s["closing_date"] and s["due_date"]]
    debt_today = Decimal(0)
    cycles: list[_Cycle] = []
    for i, s in enumerate(statements):
        closed = s["state"] in ("closed", "closed_estimated")
        installments = Decimal(s["projected_installments"])
        recurring_total = Decimal(s["projected_recurring"])
        real = Decimal(s["total"]) - installments - recurring_total if not closed else Decimal(s["total"])
        unpaid = Decimal(s["total"]) - Decimal(s["paid"]) if closed and s["status"] in ("due", "overdue") else Decimal(0)
        if closed:
            debt_today += max(unpaid, Decimal(0))
        elif s["state"] == "open":
            debt_today += real
        opens = statements[i - 1]["closing_date"] if i > 0 else add_months(s["closing_date"], -1)
        cycles.append(_Cycle(
            opens=opens,
            closing=s["closing_date"],
            due=s["due_date"],
            known_total=Decimal(s["total"]),
            real=real if s["state"] == "open" else Decimal(0),
            recurring=[
                (item["date"], Decimal(item["amount"]))
                for item in s.get("projected_items") or []
                if item.get("source") == "recurrence" and item.get("date")
            ],
            unpaid=max(unpaid, Decimal(0)),
            state=s["state"],
        ))
    rows = (
        await session.execute(
            select(Transaction).where(
                Transaction.account_id == account.id,
                Transaction.is_ignored.is_(False),
                Transaction.source != "opening_balance",
                Transaction.date <= today,
            )
        )
    ).scalars().all()
    walk = [
        (tx.date, signed_account_amount(tx, account))
        for tx in rows
        if tx.card_line_kind in ("purchase", "credit", "payment")
    ]
    return CardBillDebt(today=today, debt_today=debt_today, rows=walk, cycles=cycles)
