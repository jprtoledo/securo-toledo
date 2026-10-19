"""Credit-card statement view, one entry per due month (RFC toledo-features §6.4).

Pure: built from bills, the card's live rows (already classified by card line
kind and placed by effective date) and projections.

- Only ``purchase``/``credit`` rows count as spending; ``payment`` rows are
  payments; ``previous_balance`` rows (and carried-over credits) are the
  balance carried from the previous bill.
- Ledger banks (a statement holding a previous-balance row, e.g. CAIXA): the
  computed total is the sum of all its rows. Otherwise (e.g. Sicredi):
  spending minus what was prepaid during the cycle.
- Payments are allocated from payment rows only (Pluggy's ``bill.payments``
  differs per bank; it is kept for display and the settlement cross-check):
  a payment between closing N and closing N+1 settles statement N up to its
  total; the excess is prepayment of N+1.
- Securo's synthetic finance-charge rows count as purchases of their bill.
- Pluggy's settlement formula ``total(N) + financeCharges(N+1) =
  payments(N+1)`` is a cross-check: when it holds, N is paid even without
  payment rows.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from app.services.recompute.billing_cycle import CycleDays, KnownBill, estimated_dates
from app.services.recompute.months import add_months, month_of

CENT = Decimal("0.01")
ZERO = Decimal("0")


def q(value) -> Decimal:
    return Decimal(value).quantize(CENT)


@dataclass
class StatementRow:
    id: object
    date: date
    month: date  # due month (month of the effective date)
    signed_amount: Decimal  # account currency, − purchase / + credit
    kind: str  # purchase | credit | payment | previous_balance
    type: str = "debit"
    operation_type: Optional[str] = None


@dataclass
class Projection:
    month: date
    amount: Decimal  # positive = adds to the bill
    source: str = "installment"  # installment | recurrence
    # What the line is, for listing it (description, number/total of a
    # parcel, plan or recurrence id, category, expected date).
    label: Optional[dict] = None


@dataclass
class PaymentRef:
    date: date
    amount: Decimal


@dataclass
class Statement:
    month: date
    state: str
    status: str
    bill_id: Optional[object]
    closing_date: Optional[date]
    due_date: Optional[date]
    dates_estimated: bool
    total: Decimal
    total_estimated: bool
    posted_total: Decimal
    spent: Decimal
    prepaid: Decimal
    paid: Decimal
    paid_at: Optional[date]
    payments: list[PaymentRef]
    minimum_payment: Optional[Decimal]
    finance_charges: Decimal
    reconciliation: Optional[dict]
    history: Optional[str]
    purchase_count: int
    projected_installments: Decimal = ZERO
    projected_installment_count: int = 0
    projected_recurring: Decimal = ZERO
    projected_recurring_count: int = 0
    provider_payments: list = field(default_factory=list)
    projected_items: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def find_carried_over_credits(rows: list[StatementRow], ledger_months: set) -> set:
    """CAIXA re-sends in bill N the credits already in bill N−1 (same date
    and amount, the text may change), compensated by an inflated previous
    balance. Matched 1:1 by (month − 1, date, amount); only in ledger
    statements, where the previous balance line is what compensates them."""
    def key(r: StatementRow, month: date):
        return (month, r.date, r.signed_amount)

    available: dict = {}
    for r in rows:
        if r.type == "credit" and r.kind == "credit":
            k = key(r, r.month)
            available[k] = available.get(k, 0) + 1
    carried = set()
    for r in sorted(rows, key=lambda r: str(r.id)):
        if r.type != "credit" or r.kind != "credit" or r.month not in ledger_months:
            continue
        prev = key(r, add_months(r.month, -1))
        if available.get(prev, 0) > 0:
            carried.add(r.id)
            available[prev] -= 1
    return carried


def find_payment_echoes(rows: list[StatementRow]) -> set:
    """Pluggy's synthesized "Pagamento recebido" (PAGAMENTO_FATURA) on a
    ledger statement that already lists the bank's own payment line: a
    receipt, not a ledger line — counted, the payment would enter twice."""
    ledger_months = {r.month for r in rows if r.kind == "previous_balance"}
    return {
        r.id for r in rows
        if r.month in ledger_months
        and r.kind == "payment"
        and (r.operation_type or "").upper() == "PAGAMENTO_FATURA"
    }


def open_month_for(today: date, bills_by_month: dict, cycle: Optional[CycleDays]) -> date:
    """Due month of the bill still open today: the first whose closing is
    after today."""
    for i in range(-1, 4):
        month = add_months(month_of(today), i)
        bill = bills_by_month.get(month)
        closing = bill.closing_date if bill and bill.closing_date else (
            estimated_dates(month, cycle)[0] if cycle else None
        )
        if closing and closing > today:
            return month
    return month_of(today)


def build_statements(
    *,
    bills: list[KnownBill],
    rows: list[StatementRow],
    cycle: Optional[CycleDays],
    today: date,
    projected: Optional[list[Projection]] = None,
) -> list[Statement]:
    projected = projected or []
    bills_by_month = {b.month: b for b in sorted(bills, key=lambda b: b.due_date)}
    months_present = set(bills_by_month) | {r.month for r in rows} | {p.month for p in projected}
    if not months_present:
        return []
    first, last = min(months_present), max(months_present)
    months = []
    m = first
    while m <= last:
        months.append(m)
        m = add_months(m, 1)
    history_start = min((r.month for r in rows), default=None)
    open_month = open_month_for(today, bills_by_month, cycle)

    def dates_for(month: date):
        bill = bills_by_month.get(month)
        est = estimated_dates(month, cycle) if cycle else (None, None)
        closing = (bill.closing_date if bill and bill.closing_date else est[0])
        due = bill.due_date if bill else est[1]
        estimated = bill is None or bill.closing_estimated or bill.closing_date is None
        return closing, due, estimated

    payments = sorted(
        (PaymentRef(r.date, abs(r.signed_amount)) for r in rows if r.kind == "payment"),
        key=lambda p: p.date,
    )

    def payments_between(start: Optional[date], end: Optional[date]) -> list[PaymentRef]:
        if start is None or end is None:
            return []
        return [p for p in payments if start <= p.date < end]

    out: list[Statement] = []
    prev: Optional[Statement] = None
    for month in months:
        bill = bills_by_month.get(month)
        closing, due, estimated = dates_for(month)
        next_closing = dates_for(add_months(month, 1))[0]
        own = [r for r in rows if r.month == month]
        regular = [r for r in own if r.kind in ("purchase", "credit")]
        spent = q(-sum((r.signed_amount for r in regular), ZERO))

        cycle_paid = sum((p.amount for p in payments_between(prev.closing_date if prev else None, closing)), ZERO)
        prepaid = q(max(ZERO, cycle_paid - max(ZERO, prev.total))) if prev else ZERO
        ledger = any(r.kind == "previous_balance" for r in own)
        computed = q(-sum((r.signed_amount for r in own), ZERO)) if ledger else q(spent - prepaid)

        own_projected = [p for p in projected if p.month == month]
        installments = [p for p in own_projected if p.source != "recurrence"]
        recurring = [p for p in own_projected if p.source == "recurrence"]
        projected_total = q(sum((p.amount for p in own_projected), ZERO))

        if bill is not None:
            state = "closed"
        elif closing is not None and closing <= today:
            state = "closed_estimated"
        elif month == open_month or (
            prev is not None and prev.closing_date is not None and prev.closing_date <= today
        ):
            state = "open"
        else:
            state = "future"
        is_closed = state in ("closed", "closed_estimated")
        if bill is not None:
            total = q(bill.total_amount)
        elif is_closed:
            total = computed
        else:
            total = q(computed + projected_total)

        window = payments_between(closing, next_closing or date(9999, 12, 31))
        paid = ZERO
        paid_at: Optional[date] = None
        if total > CENT:
            running = ZERO
            for p in window:
                running += p.amount
                if paid_at is None and running >= total - CENT:
                    paid_at = p.date
            paid = q(min(running, total))

        # Settlement cross-check with the next bill (Pluggy's formula).
        next_bill = bills_by_month.get(add_months(month, 1))
        if bill is not None and paid_at is None and next_bill is not None and next_bill.payments:
            provider_paid = sum((Decimal(str(p.get("amount", 0))) for p in next_bill.payments
                                 if isinstance(p, dict)), ZERO)
            charges = Decimal(next_bill.finance_charges or 0)
            if abs(total + charges - provider_paid) <= CENT and provider_paid > 0:
                dates = [p.get("paymentDate") or p.get("date") for p in next_bill.payments if isinstance(p, dict)]
                parsed = sorted(date.fromisoformat(str(d)[:10]) for d in dates if d)
                paid_at = parsed[0] if parsed else (next_bill.closing_date or next_bill.due_date)
                paid = total

        if total <= CENT:
            status = "nothing_due"
        elif paid_at is not None:
            status = "paid"
        elif not is_closed:
            status = "not_closed"
        elif history_start is None or month < history_start:
            status = "unknown"
        elif due is not None and today > due:
            status = "overdue"
        else:
            status = "due"

        statement = Statement(
            month=month,
            state=state,
            status=status,
            bill_id=bill.id if bill else None,
            closing_date=closing,
            due_date=due,
            dates_estimated=estimated,
            total=total,
            total_estimated=bill is None,
            posted_total=q(bill.total_amount) if bill is not None else computed,
            spent=spent,
            prepaid=prepaid,
            paid=paid,
            paid_at=paid_at,
            payments=window,
            minimum_payment=Decimal(bill.minimum_payment) if bill and bill.minimum_payment is not None else None,
            finance_charges=q(bill.finance_charges or 0) if bill else ZERO,
            reconciliation=(
                {"computed": computed, "diff": q(Decimal(bill.total_amount) - computed)} if bill else None
            ),
            history=(
                "before" if history_start is None or month < history_start
                else "first" if month == history_start else None
            ),
            purchase_count=sum(1 for r in own if r.kind == "purchase"),
            projected_installments=ZERO if is_closed else q(sum((p.amount for p in installments), ZERO)),
            projected_installment_count=0 if is_closed else len(installments),
            projected_recurring=ZERO if is_closed else q(sum((p.amount for p in recurring), ZERO)),
            projected_recurring_count=0 if is_closed else len(recurring),
            provider_payments=list(bill.payments) if bill else [],
            projected_items=[] if is_closed else [
                {"source": p.source, "amount": q(p.amount), **(p.label or {})} for p in own_projected
            ],
        )
        out.append(statement)
        prev = statement
    return out
