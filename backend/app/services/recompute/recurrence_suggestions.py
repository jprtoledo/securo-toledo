"""Recurring-charge suggestions from history (RFC toledo-features §8.6), pure.
Only suggests — the user creates.

- Groups: direction + currency + description key (≥ 3 letters, ≥ 40 %
  letters — codes like "TRA-5639146-…" are not merchants; amount ≥ 2).
- Fixed: clusters within 2 % (+ 0,05) — tells apart several subscriptions
  of one merchant. Variable: the rest of the group, coefficient of
  variation ≤ 40 %, tolerance = max deviation + 20 points within 40–80 %.
- Dominant day: the day with most rows within ±window (circular month
  distance); one row per period, the closest to that day.
- Requires ≥ 3 of the last 6 months, the last occurrence ≤ 45 days ago,
  ≤ 1.6 rows per period (groceries, food delivery, PIX to people are out),
  and uses the most recent run with gaps ≤ 2 months.
- Yearly: the same amount ±5 %, 11–13 months apart, nothing similar between.
- Weekly: ≥ 6 occurrences in the last 10 weeks on the same weekday ±1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from statistics import mean, pstdev
from typing import Optional

from app.services.recompute.months import add_months, clamp_day, month_diff, month_of
from app.services.rule_engine import description_key

FIXED_TOLERANCE = Decimal("0.02")
VARIABLE_MAX_CV = 0.4
RECENT_MONTHS = 6
MIN_RECENT = 3
MAX_PER_PERIOD = 1.6
LAST_SEEN_DAYS = 45
MAX_GAP = 2
MIN_AMOUNT = Decimal("2")
MIN_LETTER_RATIO = 0.4
CENT = Decimal("0.01")


@dataclass
class SuggestionRow:
    id: object
    date: date
    description: str
    direction: str  # debit | credit
    currency: str
    amount: Decimal


@dataclass
class Suggestion:
    key: str
    description: str
    type: str
    currency: str
    amount_mode: str
    amount: Decimal
    amount_tolerance_pct: Decimal
    frequency: str
    day_of_month: Optional[int]
    day_window: int
    start_date: date
    match_condition: dict
    occurrences: list = field(default_factory=list)
    periods_covered: int = 0
    billing_weekday: Optional[int] = None


def looks_like_code(text: str) -> bool:
    compact = "".join(text.split())
    return not compact or len(description_key(text)) / len(compact) < MIN_LETTER_RATIO


def _day_dist(a: int, b: int) -> float:
    d = abs(a - b)
    return min(d, 30.44 - d)


def _dominant_day(rows: list[SuggestionRow], window: int) -> int:
    best, best_count = rows[-1].date.day, -1
    for r in reversed(rows):
        count = sum(1 for x in rows if _day_dist(x.date.day, r.date.day) <= window)
        if count > best_count:
            best, best_count = r.date.day, count
    return best


def _period_for(d: date, day: int) -> date:
    m = month_of(d)
    return min((add_months(m, -1), m, add_months(m, 1)),
               key=lambda p: abs((d - clamp_day(p, day)).days))


def _keep_per_period(rows, day: int, window: int):
    by_period: dict = {}
    for r in rows:
        p = _period_for(r.date, day)
        dist = abs((r.date - clamp_day(p, day)).days)
        if dist > window:
            continue
        cur = by_period.get(p)
        if cur is None or dist < abs((cur.date - clamp_day(p, day)).days):
            by_period[p] = r
    return sorted(by_period.items())


def _run_start(periods: list[date]) -> date:
    start = periods[-1]
    for i in range(len(periods) - 2, -1, -1):
        if month_diff(periods[i + 1], periods[i]) > MAX_GAP + 1:
            break
        start = periods[i]
    return start


def _cluster_by_amount(rows):
    clusters: list[list[SuggestionRow]] = []
    for r in sorted(rows, key=lambda r: r.amount):
        if clusters and r.amount <= clusters[-1][0].amount * (1 + FIXED_TOLERANCE) + Decimal("0.05"):
            clusters[-1].append(r)
        else:
            clusters.append([r])
    return [sorted(c, key=lambda r: r.date) for c in clusters]


def _cv(values: list[float]) -> float:
    m = mean(values)
    return float("inf") if not m else pstdev(values) / m


def _variable_tolerance(amounts: list[Decimal]) -> Decimal:
    m = sum(amounts) / len(amounts)
    max_dev = max(abs(a - m) / m for a in amounts)
    return Decimal(min(80, max(40, round(float(max_dev) * 100) + 20)))


def _condition(key: str) -> dict:
    return {"op": "and", "conditions": [{"field": "description_key", "op": "starts_with", "value": key}]}


def suggest(rows: list[SuggestionRow], today: date, dismissed: set[str] = frozenset()) -> list[Suggestion]:
    recent_from = add_months(month_of(today), -RECENT_MONTHS)
    groups: dict = {}
    for r in rows:
        k = description_key(r.description)
        if len(k) < 3 or r.amount < MIN_AMOUNT or looks_like_code(r.description):
            continue
        groups.setdefault((r.direction, r.currency, k), []).append(r)

    out: list[Suggestion] = []

    def build(gkey, members, mode, group_size):
        if len(members) < MIN_RECENT:
            return None
        base_window = 5 if mode == "fixed" else 8
        day = _dominant_day(members, base_window)
        kept = _keep_per_period(members, day, base_window)
        periods = [p for p, _ in kept]
        recent = sum(1 for p in periods if p >= recent_from)
        last = kept[-1][1] if kept else None
        if recent < MIN_RECENT or last is None or (today - last.date).days > LAST_SEEN_DAYS:
            return None
        if group_size / len(kept) > MAX_PER_PERIOD:
            return None
        start = _run_start(periods)
        run = [(p, r) for p, r in kept if p >= start]
        if len(run) < MIN_RECENT:
            return None
        amounts = [r.amount for _, r in run]
        if mode == "variable" and _cv([float(a) for a in amounts]) > VARIABLE_MAX_CV:
            return None
        direction, currency, desc_key = gkey
        expected = last.amount if mode == "fixed" else (
            sum(amounts[-3:]) / min(3, len(amounts))).quantize(CENT)
        key = f"{direction}|{currency}|{desc_key}|{mode}|{round(expected) if mode == 'fixed' else ''}|monthly"
        if key in dismissed:
            return "dismissed"
        max_dev = max(abs((r.date - clamp_day(p, day)).days) for p, r in run)
        last_period = run[-1][0]
        return Suggestion(
            key=key,
            description=last.description,
            type=direction,
            currency=currency,
            amount_mode=mode,
            amount=expected,
            amount_tolerance_pct=Decimal("2") if mode == "fixed" else _variable_tolerance(amounts),
            frequency="monthly",
            day_of_month=last.date.day if month_of(last.date) == month_of(last_period) else day,
            day_window=max(5, min(10, max_dev + 2)),
            start_date=clamp_day(start, day),
            match_condition=_condition(desc_key),
            occurrences=[{"id": r.id, "date": r.date, "amount": r.amount} for _, r in reversed(run)][:6],
            periods_covered=len(run),
        )

    for gkey, members in groups.items():
        ordered = sorted(members, key=lambda r: r.date)
        used: set = set()
        for cluster in _cluster_by_amount(ordered):
            s = build(gkey, cluster, "fixed", len(cluster))
            if not s:
                continue
            # A dismissed fixed cluster is consumed too: it never comes back as variable.
            if s != "dismissed":
                out.append(s)
            used.update(r.id for r in cluster)
        rest = [r for r in ordered if r.id not in used]
        variable = build(gkey, rest, "variable", len(rest))
        if variable and variable != "dismissed":
            out.append(variable)
        weekly = _suggest_weekly(gkey, ordered, today, dismissed) if not used and not variable else None
        if weekly:
            out.append(weekly)
        elif not used and not variable:
            yearly = _suggest_yearly(gkey, ordered, today, dismissed)
            if yearly:
                out.append(yearly)
    return sorted(out, key=lambda s: (-s.periods_covered, -s.amount))


def _suggest_yearly(gkey, rows, today, dismissed) -> Optional[Suggestion]:
    if len(rows) < 2 or len(rows) > 3:
        return None
    a, b = rows[-2], rows[-1]
    gap = month_diff(month_of(b.date), month_of(a.date))
    if gap < 11 or gap > 13 or abs(b.amount - a.amount) > a.amount * Decimal("0.05"):
        return None
    if (today - b.date).days > 400:
        return None
    direction, currency, desc_key = gkey
    key = f"{direction}|{currency}|{desc_key}|fixed|{round(b.amount)}|yearly"
    if key in dismissed:
        return None
    return Suggestion(
        key=key, description=b.description, type=direction, currency=currency, amount_mode="fixed",
        amount=b.amount, amount_tolerance_pct=Decimal("5"), frequency="yearly",
        day_of_month=b.date.day, day_window=10,
        start_date=date(a.date.year, b.date.month, min(b.date.day, 28)),
        match_condition=_condition(desc_key),
        occurrences=[{"id": r.id, "date": r.date, "amount": r.amount} for r in (b, a)],
        periods_covered=2,
    )


def _suggest_weekly(gkey, rows, today, dismissed) -> Optional[Suggestion]:
    recent = [r for r in rows if r.date >= today - timedelta(weeks=10)]
    if len(recent) < 6:
        return None
    weekday = max(range(7), key=lambda d: sum(1 for r in recent if min(abs(r.date.weekday() - d), 7 - abs(r.date.weekday() - d)) <= 1))
    on_day = [r for r in recent if min(abs(r.date.weekday() - weekday), 7 - abs(r.date.weekday() - weekday)) <= 1]
    weeks = {r.date.isocalendar()[:2] for r in on_day}
    # The whole group, not just the rows on that weekday: a merchant used
    # several times a week (food delivery) is not a weekly service.
    if len(on_day) < 6 or len(weeks) < 6 or len(recent) / len(weeks) > MAX_PER_PERIOD:
        return None
    if (today - on_day[-1].date).days > 14:
        return None
    amounts = [r.amount for r in on_day]
    spread = (max(amounts) - min(amounts)) / min(amounts) if min(amounts) else Decimal("1")
    if spread <= FIXED_TOLERANCE:
        mode, amount, tolerance = "fixed", on_day[-1].amount, Decimal("2")
    elif _cv([float(a) for a in amounts]) <= VARIABLE_MAX_CV:
        mode, amount, tolerance = "variable", (sum(amounts[-3:]) / min(3, len(amounts))).quantize(CENT), _variable_tolerance(amounts)
    else:
        return None
    direction, currency, desc_key = gkey
    key = f"{direction}|{currency}|{desc_key}|{mode}|{round(amount) if mode == 'fixed' else ''}|weekly"
    if key in dismissed:
        return None
    first = on_day[0].date
    start = first - timedelta(days=(first.weekday() - weekday) % 7)
    return Suggestion(
        key=key, description=on_day[-1].description, type=direction, currency=currency, amount_mode=mode,
        amount=amount, amount_tolerance_pct=tolerance, frequency="weekly", day_of_month=None, day_window=2,
        start_date=start, match_condition=_condition(desc_key),
        occurrences=[{"id": r.id, "date": r.date, "amount": r.amount} for r in reversed(on_day)][:6],
        periods_covered=len(weeks),
    )
