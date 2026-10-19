"""Credit-card billing cycle without typed days (RFC toledo-features §6.3).

Placing a date in the cycle uses, in order:

1. Real bill boundaries: a date falls in the bill whose period contains it.
   A bill's period ends the day before its closing date (a purchase on the
   closing day goes to the next bill). The closing date is Pluggy's when it
   sends one; otherwise it is derived from the bill's own rows (see
   ``derive_closing_dates``). The first known bill only accepts dates at most
   35 days before its closing.
2. Projection by closing/due day — derived as the mode of the last 6 bills,
   else the account's balance close/due dates, unless the user typed them.
3. If the due day is after the closing day the bill is due the month it
   closes, otherwise the next month.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Optional

from app.services.recompute.months import add_months, clamp_day, mode, month_of

MAX_CYCLE_DAYS = 35


@dataclass
class KnownBill:
    id: object
    external_id: Optional[str]
    due_date: date
    closing_date: Optional[date] = None
    # The closing date was derived from rows, not sent by the provider.
    closing_estimated: bool = False
    total_amount: object = None
    minimum_payment: object = None
    finance_charges: object = None
    payments: list = field(default_factory=list)

    @property
    def month(self) -> date:
        return month_of(self.due_date)


@dataclass(frozen=True)
class CycleDays:
    closing_day: int
    due_day: int


def derive_closing_dates(bills: list[KnownBill], row_dates_by_bill: dict) -> None:
    """Fill missing closing dates in place (Implementer's choice, §6.3).

    For a bill without a provider closing date: the day after the latest
    purchase linked to it — the cycle closed at least then — but never after
    the earliest purchase of the next bill, which is already in the next
    cycle. A bill with no rows keeps no closing date.
    """
    ordered = sorted(bills, key=lambda b: b.due_date)
    for i, bill in enumerate(ordered):
        if bill.closing_date is not None:
            continue
        dates = row_dates_by_bill.get(bill.id) or []
        if not dates:
            continue
        candidate = max(dates) + timedelta(days=1)
        if i + 1 < len(ordered):
            following = row_dates_by_bill.get(ordered[i + 1].id) or []
            if following and min(following) < candidate:
                candidate = min(following)
        if candidate > bill.due_date:
            candidate = bill.due_date
        bill.closing_date = candidate
        bill.closing_estimated = True


def infer_cycle_days(
    bills: Iterable[KnownBill],
    balance_close_date: Optional[date] = None,
    balance_due_date: Optional[date] = None,
) -> Optional[CycleDays]:
    recent = sorted(bills, key=lambda b: b.due_date)[-6:]
    due_day = mode(b.due_date.day for b in recent)
    closing_day = mode(b.closing_date.day for b in recent if b.closing_date)
    if due_day is None and balance_due_date:
        due_day = balance_due_date.day
    if closing_day is None and balance_close_date:
        closing_day = balance_close_date.day
    if due_day is None or closing_day is None:
        return None
    return CycleDays(closing_day, due_day)


def due_month_for_closing(closing: date, cycle: CycleDays) -> date:
    closing_month = month_of(closing)
    return closing_month if cycle.due_day > cycle.closing_day else add_months(closing_month, 1)


def month_for_date_in_cycle(
    d: date, bills: list[KnownBill], cycle: Optional[CycleDays]
) -> Optional[date]:
    """Due month of the bill a date falls in."""
    closed = sorted((b for b in bills if b.closing_date), key=lambda b: b.closing_date)
    for i, bill in enumerate(closed):
        if d >= bill.closing_date:
            continue
        if i > 0:
            return bill.month
        if (bill.closing_date - d).days <= MAX_CYCLE_DAYS:
            return bill.month
        break
    if cycle is None:
        return None
    month = month_of(d)
    closing_this_month = clamp_day(month, cycle.closing_day)
    closing_month = month if d < closing_this_month else add_months(month, 1)
    return due_month_for_closing(clamp_day(closing_month, cycle.closing_day), cycle)


def due_date_for_month(
    month: date, bills_by_month: dict, cycle: Optional[CycleDays]
) -> Optional[date]:
    bill = bills_by_month.get(month)
    if bill is not None:
        return bill.due_date
    if cycle is None:
        return None
    return clamp_day(month, cycle.due_day)


def estimated_dates(month: date, cycle: CycleDays) -> tuple[date, date]:
    """(closing, due) of the bill due in ``month`` from the cycle days."""
    due = clamp_day(month, cycle.due_day)
    closing_month = month if cycle.due_day > cycle.closing_day else add_months(month, -1)
    return clamp_day(closing_month, cycle.closing_day), due
