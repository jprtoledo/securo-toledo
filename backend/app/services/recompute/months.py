"""Calendar helpers on plain dates (months are the first day of the month)."""
from __future__ import annotations

import calendar
from collections import Counter
from datetime import date
from typing import Iterable, Optional, TypeVar

T = TypeVar("T")


def month_of(d: date) -> date:
    return d.replace(day=1)


def add_months(month: date, n: int) -> date:
    index = month.year * 12 + (month.month - 1) + n
    return date(index // 12, index % 12 + 1, 1)


def month_diff(a: date, b: date) -> int:
    """a − b in months."""
    return (a.year * 12 + a.month) - (b.year * 12 + b.month)


def clamp_day(month: date, day: int) -> date:
    """``day`` in ``month``, clamped to its last day (31 → 30/28)."""
    last = calendar.monthrange(month.year, month.month)[1]
    return date(month.year, month.month, min(day, last))


def mode(values: Iterable[T]) -> Optional[T]:
    """Most frequent value; ties go to the latest in the sequence."""
    counts: Counter = Counter()
    best: Optional[T] = None
    best_count = 0
    for v in values:
        counts[v] += 1
        if counts[v] >= best_count:
            best, best_count = v, counts[v]
    return best
