"""Recurrences v2 (RFC toledo-features §8), pure.

Linking, from scratch per account, period by period in chronological order
(so the expectation evolves with the confirmed charges):

1. the user's decisions first: a row manually linked goes to the period with
   the nearest anchor date → manual;
2. each active recurrence opens a slot per period (expected date + amount).
   Every (row × slot) pair inside the day window is ranked by cost —
   ``|amount − expected| / expected / tolerance`` (× 0.5 in variable mode) +
   ``|days| / window`` + 0.5 when the row is in another month than the
   period — cheapest first, one row per slot → auto when the amount fits.
   This is what tells apart several subscriptions at one merchant;
3. an empty slot + a matching row in the window whose amount does not fit →
   conflict "amount changed";
4. foreign-currency recurrences take the IOF line along (a row in the
   account currency, same merchant, ±1 day, 2–7 % of the charge) as a
   companion;
5. another fitting row for a slot already filled → conflict "double charge".
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, Iterable, Optional

from app.services.recompute.months import add_months, clamp_day, month_of
from app.services.rule_engine import description_key

MONTH_STEPS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "yearly": 12, "weekly_billed_monthly": 1}
DAY_STEPS = {"weekly": 7, "biweekly": 14}
DEFAULT_WINDOWS = {
    "weekly": 2, "biweekly": 3, "monthly": 5, "quarterly": 5, "semiannual": 5,
    "yearly": 10, "weekly_billed_monthly": 5,
}
MIN_ABS_TOLERANCE = Decimal("0.05")
VARIABLE_SAMPLE = 3
IOF_RANGE = (Decimal("0.02"), Decimal("0.07"))
CENT = Decimal("0.01")


def q(value) -> Decimal:
    return Decimal(value).quantize(CENT)


@dataclass
class RecurrenceConfig:
    id: object
    direction: str  # debit | credit
    currency: str
    amount: Decimal  # reference (per unit for weekly_billed_monthly)
    frequency: str
    start_date: date
    day_of_month: Optional[int] = None
    end_date: Optional[date] = None
    amount_mode: str = "fixed"
    tolerance_pct: Decimal = Decimal("0")
    day_window: Optional[int] = None
    weekend_adjustment: str = "none"
    billing_weekday: Optional[int] = None
    week_count_basis: Optional[str] = None
    enabled: bool = True
    skipped: frozenset = frozenset()  # periods the user marked as not happening

    @property
    def window(self) -> int:
        return self.day_window if self.day_window is not None else DEFAULT_WINDOWS.get(self.frequency, 5)

    @property
    def anchor_day(self) -> int:
        return self.day_of_month or self.start_date.day

    @property
    def month_based(self) -> bool:
        return self.frequency not in DAY_STEPS


@dataclass
class RecurrenceRow:
    id: object
    date: date
    description: str
    direction: str
    currency: str
    amount: Decimal  # absolute, row currency
    account_amount: Decimal  # absolute, account currency
    manual_id: Optional[object] = None
    manual_none: bool = False
    manual_excluded: bool = False
    rejected_ids: tuple = ()
    # A placeholder Securo generated for this recurrence (auto_generate).
    placeholder_for: Optional[object] = None


@dataclass
class Assignment:
    recurrence_id: object
    period: date
    link: str  # auto | conflict | manual
    issue: Optional[dict] = None
    companion: bool = False
    excluded: bool = False


@dataclass
class Occurrence:
    tx_id: object
    period: date
    date: date
    link: str
    companion: bool
    amount: Decimal
    account_amount: Decimal
    excluded: bool = False
    account_id: object = None  # the account that paid it


@dataclass
class Expectation:
    date: date
    amount: Decimal  # recurrence currency
    account_amount: Optional[Decimal]  # account currency estimate, None if no FX known


# --- schedule -------------------------------------------------------------------


def adjust_weekend(d: date, rule: str) -> date:
    weekday = d.weekday()
    if rule == "none" or weekday < calendar.SATURDAY:
        return d
    if rule == "previous_friday":
        return d - timedelta(days=weekday - calendar.FRIDAY)
    return d + timedelta(days=7 - weekday)


def periods_of(rec: RecurrenceConfig, start: date, end: date) -> list[date]:
    """Nominal period dates in [start, end] (the series ends at end_date)."""
    out: list[date] = []
    last = min(end, rec.end_date) if rec.end_date else end
    if rec.month_based:
        step = MONTH_STEPS.get(rec.frequency, 1)
        first_month = month_of(rec.start_date)
        k = 0
        if start > rec.start_date:
            k = max(0, ((start.year - first_month.year) * 12 + start.month - first_month.month) // step - 1)
        while True:
            p = clamp_day(add_months(first_month, k * step), rec.anchor_day)
            if p > last:
                break
            # A period before the series began (start mid-month, anchor day
            # earlier) is not owed.
            if p >= start and p >= rec.start_date - timedelta(days=rec.window):
                out.append(p)
            k += 1
            if k > 2400:
                break
    else:
        step = DAY_STEPS[rec.frequency]
        k = max(0, (start - rec.start_date).days // step - 1) if start > rec.start_date else 0
        while True:
            p = rec.start_date + timedelta(days=k * step)
            if p > last:
                break
            if p >= start:
                out.append(p)
            k += 1
            if k > 5000:
                break
    return out


def nearest_period(rec: RecurrenceConfig, d: date) -> date:
    candidates = periods_of(rec, d - timedelta(days=400), d + timedelta(days=400)) or [rec.start_date]
    return min(candidates, key=lambda p: (abs((d - p).days), p))


def units_for(rec: RecurrenceConfig, period: date) -> int:
    """weekly_billed_monthly: occurrences of the billing weekday in the basis
    month (the period's month, or the previous month)."""
    if rec.frequency != "weekly_billed_monthly" or rec.billing_weekday is None:
        return 1
    basis = month_of(period) if rec.week_count_basis == "same_month" else add_months(month_of(period), -1)
    days = calendar.monthrange(basis.year, basis.month)[1]
    return sum(1 for d in range(1, days + 1) if date(basis.year, basis.month, d).weekday() == rec.billing_weekday)


# --- expectation ------------------------------------------------------------------


def expectation_for(
    rec: RecurrenceConfig,
    period: date,
    history: list[Occurrence],
    fx_rate: Optional[Decimal] = None,
    account_currency: Optional[str] = None,
) -> Expectation:
    confirmed = sorted(
        (h for h in history if h.link != "conflict" and h.period < period),
        key=lambda h: (h.period, h.date),
    )
    mains = [h for h in confirmed if not h.companion]

    if rec.month_based:
        own_month = [m for m in mains if month_of(m.date) == month_of(m.period)]
        day = own_month[-1].date.day if own_month else rec.anchor_day
        expected_date = clamp_day(month_of(period), day)
    else:
        expected_date = period
    expected_date = adjust_weekend(expected_date, rec.weekend_adjustment)

    by_period: dict = {}
    for h in confirmed:
        by_period.setdefault(h.period, []).append(h)
    per_period = []
    for p in sorted(by_period):
        slot = by_period[p]
        if any(h.excluded for h in slot):
            continue
        main = [h for h in slot if not h.companion]
        if main:
            per_period.append((p, main[-1]))

    units = units_for(rec, period)
    amount = Decimal(rec.amount) * units
    if per_period:
        sample = per_period[-1:] if rec.amount_mode == "fixed" else per_period[-VARIABLE_SAMPLE:]
        per_unit = sum((h.amount / units_for(rec, p) for p, h in sample), Decimal("0")) / len(sample)
        amount = per_unit * units
    amount = q(amount)

    account_amount: Optional[Decimal]
    if account_currency is None or rec.currency == account_currency:
        account_amount = amount
    else:
        if per_period and per_period[-1][1].amount > 0:
            p, last = per_period[-1]
            slot_account = sum((h.account_amount for h in by_period[p]), Decimal("0"))
            account_amount = q(slot_account / last.amount * amount)
        else:
            account_amount = q(amount * fx_rate) if fx_rate else None
    return Expectation(expected_date, amount, account_amount)


def amount_fits(rec: RecurrenceConfig, expected: Decimal, actual: Decimal) -> bool:
    tolerance = max(expected * Decimal(rec.tolerance_pct) / 100, MIN_ABS_TOLERANCE)
    return abs(actual - expected) <= tolerance


def _cost(rec: RecurrenceConfig, exp: Expectation, row: RecurrenceRow, period: date) -> float:
    tol = max(float(rec.tolerance_pct) / 100, 0.01)
    amount_rel = abs(float(row.amount - exp.amount)) / max(float(exp.amount), 0.01) / tol
    day_rel = abs((row.date - exp.date).days) / max(rec.window, 1)
    crosses = 0.5 if month_of(row.date) != month_of(period) else 0.0
    return amount_rel * (1.0 if rec.amount_mode == "fixed" else 0.5) + day_rel + crosses


def _companion_key(text: str) -> str:
    return description_key(text).replace("IOF", "")


def is_companion(main: RecurrenceRow, row: RecurrenceRow, account_currency: str) -> bool:
    if row.id == main.id or row.currency == main.currency or row.currency != account_currency:
        return False
    if row.direction != main.direction or abs((row.date - main.date).days) > 1:
        return False
    a, b = _companion_key(main.description), _companion_key(row.description)
    if len(a) < 3 or len(b) < 3 or not (a == b or a.startswith(b) or b.startswith(a)):
        return False
    if main.account_amount <= 0:
        return False
    ratio = row.account_amount / main.account_amount
    return IOF_RANGE[0] <= ratio <= IOF_RANGE[1]


# --- linking ----------------------------------------------------------------------------


def link_recurrences(
    recs: list[RecurrenceConfig],
    rows: list[RecurrenceRow],
    matches: Callable[[RecurrenceConfig, RecurrenceRow], bool],
    through: date,
    account_currency: str,
) -> dict:
    """row id → Assignment."""
    out: dict = {}
    active = [r for r in recs if r.enabled]
    by_id = {r.id: r for r in active}
    history: dict = {r.id: [] for r in active}
    row_by_id = {r.id: r for r in rows}

    def free(row: RecurrenceRow) -> bool:
        return row.id not in out and not row.manual_none and row.manual_id is None and row.placeholder_for is None

    # 1) the user's decisions, and Securo's own placeholders
    manual_slots: dict = {}
    for row in rows:
        target = row.manual_id or row.placeholder_for
        rec = by_id.get(target) if target is not None else None
        if rec is None:
            continue
        period = nearest_period(rec, row.date)
        link = "manual" if row.manual_id else "auto"
        out[row.id] = Assignment(rec.id, period, link, None, False, bool(row.manual_excluded))
        manual_slots.setdefault((rec.id, period), []).append(row)

    if not active:
        return out
    first = min(r.start_date for r in active)
    candidates = {
        r.id: [
            row for row in rows
            if row.direction == r.direction and row.currency == r.currency
            and r.id not in row.rejected_ids and matches(r, row)
        ]
        for r in active
    }
    periods_by_month: dict = {}
    for rec in active:
        for p in periods_of(rec, first, through):
            periods_by_month.setdefault(month_of(p), []).append((rec, p))

    for month in sorted(periods_by_month):
        slots = []
        for rec, period in sorted(periods_by_month[month], key=lambda x: (x[1], str(x[0].id))):
            exp = expectation_for(rec, period, history[rec.id], None, account_currency)
            slots.append({"rec": rec, "period": period, "exp": exp,
                          "filled": list(manual_slots.get((rec.id, period), []))})

        pairs = []
        for s in slots:
            rec, exp = s["rec"], s["exp"]
            for row in candidates[rec.id]:
                if not free(row) or abs((row.date - exp.date).days) > rec.window:
                    continue
                pairs.append({
                    "s": s, "row": row, "cost": _cost(rec, exp, row, s["period"]),
                    "fits": amount_fits(rec, exp.amount, row.amount),
                })
        pairs.sort(key=lambda p: (p["cost"], p["row"].date, str(p["row"].id)))

        for p in pairs:  # 2) fits → auto
            s, row = p["s"], p["row"]
            if not p["fits"] or s["filled"] or row.id in out:
                continue
            out[row.id] = Assignment(s["rec"].id, s["period"], "auto")
            s["filled"].append(row)

        by_day = sorted(pairs, key=lambda p: (abs((p["row"].date - p["s"]["exp"].date).days), str(p["row"].id)))
        for p in by_day:  # 3) empty slot, amount off → conflict
            s, row = p["s"], p["row"]
            if p["fits"] or s["filled"] or row.id in out:
                continue
            out[row.id] = Assignment(s["rec"].id, s["period"], "conflict", {
                "code": "amount_changed",
                "charged": str(q(row.amount)),
                "expected": str(s["exp"].amount),
            })
            s["filled"].append(row)

        for s in slots:  # 4) IOF companions
            if s["rec"].currency == account_currency:
                continue
            for main in list(s["filled"]):
                main_assignment = out.get(main.id)
                if main_assignment is None:
                    continue
                for row in rows:
                    if not free(row) or s["rec"].id in row.rejected_ids or not is_companion(main, row, account_currency):
                        continue
                    out[row.id] = Assignment(
                        s["rec"].id, s["period"],
                        "conflict" if main_assignment.link == "conflict" else "auto",
                        None, True, main_assignment.excluded,
                    )

        for p in pairs:  # 5) a second fitting row → double charge?
            s, row = p["s"], p["row"]
            if not p["fits"] or row.id in out or not s["filled"]:
                continue
            out[row.id] = Assignment(s["rec"].id, s["period"], "conflict", {"code": "double_charge"})

        for s in slots:
            for row_id, a in out.items():
                if a.recurrence_id == s["rec"].id and a.period == s["period"]:
                    if any(h.tx_id == row_id for h in history[s["rec"].id]):
                        continue
                    row = row_by_id[row_id]
                    history[s["rec"].id].append(Occurrence(
                        row_id, s["period"], row.date, a.link, a.companion,
                        row.amount, row.account_amount, a.excluded,
                    ))
    return out


# --- timeline -------------------------------------------------------------------------


@dataclass
class TimelineSlot:
    period: date
    status: str  # posted | conflict | expected | late | missed | skipped
    expected_date: date
    month: date  # effective month (bill month on cards)
    amount: Optional[Decimal]  # account currency: actual (linked) or expected
    expected_amount: Decimal
    transaction_ids: list = field(default_factory=list)
    conflict_ids: list = field(default_factory=list)
    excluded: bool = False
    account_id: object = None  # the account that paid it (posted/conflict)


def build_timeline(
    rec: RecurrenceConfig,
    linked: list[Occurrence],
    *,
    today: date,
    through: date,
    month_for_date: Callable[[date], date],
    open_month: date,
    account_currency: str,
    fx_rate: Optional[Decimal] = None,
) -> list[TimelineSlot]:
    out = []
    for period in periods_of(rec, rec.start_date, through):
        exp = expectation_for(rec, period, linked, fx_rate, account_currency)
        own = [o for o in linked if o.period == period]
        if own:
            mains = sorted((o for o in own if not o.companion), key=lambda o: o.date) or own
            conflict = any(o.link == "conflict" for o in own)
            out.append(TimelineSlot(
                period, "conflict" if conflict else "posted", exp.date, month_for_date(mains[0].date),
                q(sum((o.account_amount for o in own), Decimal("0"))), exp.amount,
                [o.tx_id for o in own],
                [o.tx_id for o in own if o.link == "conflict" and not o.companion],
                any(o.excluded for o in own),
                mains[0].account_id,
            ))
            continue
        if not rec.enabled:
            continue
        month = month_for_date(exp.date)
        window_end = exp.date + timedelta(days=rec.window)
        if period in rec.skipped:
            status = "skipped"
        elif window_end >= today:
            status = "expected"
        elif month >= open_month:
            status = "late"
        else:
            status = "missed"
        out.append(TimelineSlot(period, status, exp.date, month, exp.account_amount, exp.amount))
    return out


def timeline_alerts(timeline: list[TimelineSlot]) -> dict:
    settled = [s for s in timeline if s.status not in ("expected", "late")]
    streak = 0
    for s in reversed(settled):
        if s.status != "missed":
            break
        streak += 1
    return {
        "conflicts": sum(1 for s in timeline if s.status == "conflict"),
        "last_missed": settled[-1].period if streak else None,
        "missed_streak": streak,
        "suggest_end": streak >= 2,
    }


def forecast(timeline: Iterable[TimelineSlot]) -> list[TimelineSlot]:
    """What is still to come: expected and late slots (computed, not materialized)."""
    return [s for s in timeline if s.status in ("expected", "late")]
