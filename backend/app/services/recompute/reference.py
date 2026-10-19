"""Reference month (effective date) of a credit-card row (RFC §6.2).

``effective_date`` stays the single column every report reads; the source
says why:

1. ``manual``    — the user moved the row to a bill (effective_bill_date);
2. ``bill``      — the row is linked to a stored bill: its due date (the
                   normal case for every posted card row);
3. open-bill / future rows, first method that yields a value, in the
   account's calibrated order:
   ``forecast``  — Pluggy's billForecastDate (+ learned month offset),
   ``post_date`` — billPostDate placed in the cycle (future parcels),
   ``cycle``     — the purchase date placed in the cycle;
4. ``tx_date``   — nothing else (and always for bank accounts).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from app.services.recompute.billing_cycle import (
    CycleDays,
    KnownBill,
    due_date_for_month,
    month_for_date_in_cycle,
)
from app.services.recompute.months import add_months, mode, month_diff, month_of

FALLBACKS = ("forecast", "post_date", "cycle")
PRIOR_ACCURACY = {"forecast": 0.9, "post_date": 0.8, "cycle": 0.7}
MIN_SAMPLES = 5


@dataclass
class ResolvableRow:
    date: date
    effective_bill_date: Optional[date] = None
    bill: Optional[KnownBill] = None
    bill_forecast_month: Optional[date] = None
    bill_post_date: Optional[date] = None


@dataclass
class CardContext:
    bills: list[KnownBill]
    cycle: Optional[CycleDays]
    forecast_offset_months: int = 0
    fallback_order: tuple = FALLBACKS
    bills_by_month: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.bills_by_month:
            self.bills_by_month = {b.month: b for b in sorted(self.bills, key=lambda b: b.due_date)}


@dataclass(frozen=True)
class Resolution:
    effective_date: date
    source: str


def _candidate_month(method: str, row: ResolvableRow, ctx: CardContext, offset: int) -> Optional[date]:
    if method == "forecast":
        return add_months(row.bill_forecast_month, offset) if row.bill_forecast_month else None
    if method == "post_date":
        return month_for_date_in_cycle(row.bill_post_date, ctx.bills, ctx.cycle) if row.bill_post_date else None
    return month_for_date_in_cycle(row.date, ctx.bills, ctx.cycle)


def resolve(row: ResolvableRow, ctx: Optional[CardContext]) -> Resolution:
    """``ctx`` None = a bank account (effective date = date)."""
    if row.effective_bill_date is not None:
        return Resolution(row.effective_bill_date, "manual")
    if ctx is None:
        return Resolution(row.date, "tx_date")
    if row.bill is not None:
        return Resolution(row.bill.due_date, "bill")
    for method in ctx.fallback_order:
        month = _candidate_month(method, row, ctx, ctx.forecast_offset_months)
        if month is None:
            continue
        due = due_date_for_month(month, ctx.bills_by_month, ctx.cycle)
        if due is not None:
            return Resolution(due, method)
    return Resolution(row.date, "tx_date")


def calibrate(rows: list[ResolvableRow], ctx: CardContext) -> dict:
    """Learn from rows that have a real bill (the ground truth): the month
    offset of Pluggy's forecast, and each fallback's hit rate. Methods with
    at least MIN_SAMPLES samples are ordered by measured accuracy, the rest
    by the prior forecast > post_date > cycle; ties keep that order."""
    truth = [(r, r.bill.month) for r in rows if r.bill is not None]
    offsets = [month_diff(m, r.bill_forecast_month) for r, m in truth if r.bill_forecast_month]
    offset = mode(offsets) or 0
    methods = {}
    for method in FALLBACKS:
        samples = hits = 0
        for r, month in truth:
            candidate = _candidate_month(method, r, ctx, offset)
            if candidate is None:
                continue
            samples += 1
            hits += candidate == month
        methods[method] = {"samples": samples, "hits": hits}

    def score(m: str) -> float:
        s = methods[m]
        return s["hits"] / s["samples"] if s["samples"] >= MIN_SAMPLES else PRIOR_ACCURACY[m]

    order = sorted(FALLBACKS, key=lambda m: -score(m))  # sorted() is stable
    return {
        "truth_samples": len(truth),
        "forecast_offset_months": offset,
        "methods": methods,
        "fallback_order": order,
    }


def month_key(d: date) -> date:
    return month_of(d)
