"""Installment plans (RFC toledo-features §7.2), pure.

Open Finance sends no group id for a purchase split in parcels, so parcels
are grouped heuristically per card:

1. a row is a parcel when its payment type is not SINGLE, it has more than
   one installment and a number ≥ 1;
2. group key: the purchase date and the number of installments; without a
   purchase date, the normalized description (installment markers stripped
   by the ``strip_installment_marker`` rules, accents and punctuation gone),
   the number of installments and the origin month (reference month − (n − 1));
3. two parcels with the same number in one group (three 6x purchases at the
   same shop the same day) split the group by amount, from the highest
   number down (middle parcels carry no cent adjustment);
4. parcels of one purchase differ by at most (N + 1) cents;
5. the plan's first month is the mode of (month − (n − 1));
6. each parcel is validated: in month first + (k − 1) (MONTH issue) and
   within tolerance of the median parcel (AMOUNT issue). Failing parcels stay
   in the plan as conflicts.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable, Optional

from app.services.recompute.duplicates import installment_tolerance
from app.services.recompute.months import add_months, mode
from app.services.rule_engine import strip_description


@dataclass
class ParcelRow:
    id: object
    description: str
    amount: Decimal  # absolute, account currency
    installment_number: Optional[int]
    total_installments: Optional[int]
    month: date  # reference (due) month
    date: date
    purchase_date: Optional[date] = None
    payment_type: Optional[str] = None
    strip_patterns: list[str] = field(default_factory=list)
    pending: bool = False


def is_installment(row: ParcelRow) -> bool:
    return (
        (row.payment_type or "").upper() != "SINGLE"
        and (row.total_installments or 0) > 1
        and (row.installment_number or 0) >= 1
    )


def normalize_description(text: str, patterns: list[str]) -> str:
    stripped = strip_description(text or "", patterns)
    plain = "".join(
        c for c in unicodedata.normalize("NFKD", stripped) if not unicodedata.combining(c)
    ).lower()
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def display_description(row: ParcelRow) -> str:
    return strip_description(row.description or "", row.strip_patterns) or row.description


def within_tolerance(a: Decimal, b: Decimal, total: int) -> bool:
    return abs(abs(a) - abs(b)) <= installment_tolerance(total)


def origin_month(row: ParcelRow) -> date:
    return add_months(row.month, -((row.installment_number or 1) - 1))


@dataclass
class Group:
    key: str
    members: list[ParcelRow]
    first_month: date
    total_installments: int


def group_installments(rows: list[ParcelRow]) -> list[Group]:
    base: dict[str, list[ParcelRow]] = {}
    for row in rows:
        if not is_installment(row):
            continue
        if row.purchase_date is not None:
            key = f"pd:{row.purchase_date.isoformat()}|{row.total_installments}"
        else:
            desc = normalize_description(row.description, row.strip_patterns)
            key = f"desc:{desc}|{row.total_installments}|{origin_month(row).isoformat()[:7]}"
        base.setdefault(key, []).append(row)

    groups: list[Group] = []
    for key, members in base.items():
        total = members[0].total_installments
        numbers = [m.installment_number for m in members]
        if len(set(numbers)) == len(numbers):
            groups.append(Group(key, members, mode(origin_month(m) for m in members), total))
            continue
        clusters: list[tuple[Decimal, list[ParcelRow]]] = []
        for m in sorted(members, key=lambda r: (-(r.installment_number or 0), str(r.id))):
            for amount, cluster in clusters:
                if within_tolerance(amount, m.amount, total) and all(
                    c.installment_number != m.installment_number for c in cluster
                ):
                    cluster.append(m)
                    break
            else:
                clusters.append((m.amount, [m]))
        for amount, cluster in clusters:
            groups.append(Group(
                f"{key}|{amount.quantize(Decimal('0.01'))}",
                cluster,
                mode(origin_month(m) for m in cluster),
                total,
            ))
    return groups


def median_amount(members: list[ParcelRow]) -> Decimal:
    amounts = sorted(m.amount for m in members)
    return amounts[len(amounts) // 2]


def validate_group(members: list[ParcelRow], first_month: date, total: int) -> dict:
    """id → issue dict or None: {"code": "month", "expected": "YYYY-MM-01"}
    or {"code": "amount", "expected": "12.34"}."""
    reference = median_amount(members)
    out: dict = {}
    for m in members:
        expected_month = add_months(first_month, (m.installment_number or 1) - 1)
        if m.month != expected_month:
            out[m.id] = {"code": "month", "expected": expected_month.isoformat()}
        elif not within_tolerance(m.amount, reference, total):
            out[m.id] = {"code": "amount", "expected": str(reference.quantize(Decimal("0.01")))}
        else:
            out[m.id] = None
    return out


def derive_plan_fields(members: list[ParcelRow], total: Optional[int] = None) -> dict:
    latest = max(members, key=lambda m: (m.installment_number or 0, m.date))
    first = next((m for m in members if m.installment_number == 1), None)
    return {
        "description": display_description(latest)[:500],
        "total_installments": total or mode(m.total_installments for m in members),
        "installment_amount": latest.amount.quantize(Decimal("0.01")),
        "first_month": mode(origin_month(m) for m in members),
        "purchase_date": latest.purchase_date or (first.date if first else None),
    }


@dataclass
class PlannedParcel:
    number: int
    month: date
    amount: Decimal
    kind: str  # projected | missing | before_history


def project_plan(
    *,
    total_installments: int,
    first_month: date,
    installment_amount: Decimal,
    known_numbers: set[int],
    open_month: date,
    history_start: Optional[date],
) -> list[PlannedParcel]:
    """Parcels not received yet: projected from the open bill on for numbers
    after the last one received; parcels missing in closed months are
    reported, never projected — ``missing`` (the bank did not send it) or
    ``before_history`` (before the first synced row)."""
    max_known = max(known_numbers, default=0)
    out = []
    for n in range(1, total_installments + 1):
        if n in known_numbers:
            continue
        month = add_months(first_month, n - 1)
        if n > max_known and month >= open_month:
            out.append(PlannedParcel(n, month, installment_amount, "projected"))
        elif month < open_month:
            kind = "before_history" if history_start is None or month < history_start else "missing"
            out.append(PlannedParcel(n, month, installment_amount, kind))
    return out


Strip = Callable[[object], list[str]]
