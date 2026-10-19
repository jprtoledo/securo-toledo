"""Pending copies of posted card rows (RFC §4.7, non-destructive).

Some banks (Sicredi) return a purchase twice with different ids: first
PENDING without a bill (the recent-transactions endpoint), later POSTED with
the bill id — and Pluggy never removes the pending copy. Each pending row
without a bill link is matched to a posted row with a bill link:

1. same signed amount and same installment number (0 for none), at most
   5 days apart; or the same purchase date and the same n/N with the amount
   within the installment cent tolerance (the bank sometimes moves cents);
   one-to-one, closest pair first, ties by id;
2. extra pending copies (the bank sent the pending 2+ times) with the same
   purchase timestamp to the second, the same n/N and the amount within
   tolerance as a posted row are copies too (N:1).

Posted rows that are payment echoes (§6.5) are not ledger lines and never
absorb a pending row.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

MAX_DAYS = 5


def installment_tolerance(total_installments: Optional[int]) -> Decimal:
    """Parcels of one purchase differ by at most (N + 1) cents: rounding of
    total/N lands on one parcel (Kiwify 12x: 7,03 + 11 × 6,92)."""
    n = total_installments or 0
    return Decimal(n + 1) / Decimal(100) if n > 1 else Decimal("0")


def within_tolerance(a: Decimal, b: Decimal, total_installments: Optional[int]) -> bool:
    return abs(abs(a) - abs(b)) <= installment_tolerance(total_installments)


@dataclass(frozen=True)
class DedupRow:
    id: object
    date: date
    signed_amount: Decimal
    currency: str
    status: str
    has_bill_link: bool
    installment_number: Optional[int] = None
    total_installments: Optional[int] = None
    purchase_date: Optional[date] = None
    transaction_at: Optional[datetime] = None
    # Excluded from the matching entirely (removed by the provider).
    removed: bool = False
    # The user said "not a duplicate": never flagged.
    override: bool = False


def _days(a: date, b: date) -> int:
    return abs((a - b).days)


def _same_second(a: Optional[datetime], b: Optional[datetime]) -> bool:
    if a is None or b is None:
        return False
    if (a.tzinfo is None) != (b.tzinfo is None):
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return a.replace(microsecond=0) == b.replace(microsecond=0)


def find_pending_duplicates(
    rows: list[DedupRow], not_ledger: frozenset | set = frozenset()
) -> dict[object, object]:
    """Map pending row id → the posted row id it duplicates."""
    posted = [r for r in rows if r.has_bill_link and not r.removed and r.id not in not_ledger]
    pending = [
        r for r in rows
        if not r.has_bill_link and r.status == "pending" and not r.removed and not r.override
    ]

    candidates: list[tuple[int, str, str, object, object]] = []
    for p in pending:
        for q in posted:
            if p.currency != q.currency:
                continue
            same_number = (p.installment_number or 0) == (q.installment_number or 0)
            if p.signed_amount == q.signed_amount and same_number:
                distance = _days(p.date, q.date)
                if distance <= MAX_DAYS:
                    candidates.append((distance, str(p.id), str(q.id), p.id, q.id))
                    continue
            if (
                p.purchase_date is not None
                and p.installment_number
                and p.purchase_date == q.purchase_date
                and p.installment_number == q.installment_number
                and p.total_installments == q.total_installments
                and p.signed_amount != q.signed_amount
                and (p.signed_amount < 0) == (q.signed_amount < 0)
                and within_tolerance(p.signed_amount, q.signed_amount, p.total_installments)
            ):
                distance = MAX_DAYS + _days(p.date, q.date)
                candidates.append((distance, str(p.id), str(q.id), p.id, q.id))

    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    result: dict[object, object] = {}
    used_posted: set[object] = set()
    for _, _, _, pid, qid in candidates:
        if pid in result or qid in used_posted:
            continue
        result[pid] = qid
        used_posted.add(qid)

    # Extra copies of the same purchase: N:1 by timestamp to the second.
    for p in sorted(pending, key=lambda r: str(r.id)):
        if p.id in result or p.transaction_at is None:
            continue
        for q in sorted(posted, key=lambda r: str(r.id)):
            if (
                p.currency == q.currency
                and _same_second(p.transaction_at, q.transaction_at)
                and p.installment_number == q.installment_number
                and p.total_installments == q.total_installments
                and (p.signed_amount < 0) == (q.signed_amount < 0)
                and (
                    p.signed_amount == q.signed_amount
                    or within_tolerance(p.signed_amount, q.signed_amount, p.total_installments)
                )
            ):
                result[p.id] = q.id
                break
    return result
