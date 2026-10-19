"""Reports with Pluggy credit cards (follow-up to RFC toledo-features §6).

Every report tab reads a card the way its own page does: by statement
(reference) month, without the statement lines that are not spending, with
refunds lowering spending, parcels still to come in the forecast — and the
cash flow shows money in the accounts, with each bill leaving on its due date.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.services import report_service
from app.services.card_liability_service import CardBillDebt, _Cycle
from app.services.recompute.months import add_months
from app.services.recompute.statements import Projection
from app.services.transfer_detection_service import pair_card_bill_payments

TODAY = date.today()


def _py_to_char(value, fmt):
    """Postgres to_char(date, fmt) for the formats the reports use (SQLite)."""
    if value is None:
        return None
    y, m, d = str(value)[:10].split("-")
    return {"YYYY-MM-DD": f"{y}-{m}-{d}", "YYYY-MM": f"{y}-{m}", "YYYY": y}.get(fmt, f"{y}-{m}-{d}")


@pytest.fixture(autouse=True)
async def _sqlite_to_char(session: AsyncSession):
    raw = await session.connection()
    await raw.run_sync(lambda c: c.connection.dbapi_connection.create_function("to_char", 2, _py_to_char))
    yield
THIS_MONTH = TODAY.replace(day=1)


def month_label(d: date) -> str:
    return d.strftime("%Y-%m")


def prev_month(d: date) -> date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


@pytest.fixture
async def card(session: AsyncSession, test_user, test_workspace) -> Account:
    conn = BankConnection(user_id=test_user.id, workspace_id=test_workspace.id, provider="pluggy",
                          external_id="item", institution_name="Bank", credentials={"item_id": "item"})
    session.add(conn)
    await session.flush()
    acc = Account(user_id=test_user.id, workspace_id=test_workspace.id, connection_id=conn.id,
                  name="Card", type="credit_card", balance=D("0"), currency="BRL")
    session.add(acc)
    await session.commit()
    return acc


@pytest.fixture
async def bank(session: AsyncSession, test_user, test_workspace) -> Account:
    acc = Account(user_id=test_user.id, workspace_id=test_workspace.id, name="Bank", type="checking",
                  balance=D("0"), currency="BRL")
    session.add(acc)
    await session.commit()
    return acc


async def add(session, account, desc, amount, d, type_="debit", **kw) -> Transaction:
    tx = Transaction(user_id=account.user_id, workspace_id=account.workspace_id, account_id=account.id,
                     description=desc, amount=D(amount), currency="BRL", date=d, type=type_,
                     source=kw.pop("source", "sync"), **kw)
    session.add(tx)
    await session.commit()
    return tx


def card_line(due: date, kind: str):
    """Columns recompute writes on a Pluggy card row."""
    return {"effective_date": due, "effective_date_source": "bill", "card_line_kind": kind}


@pytest.mark.asyncio
async def test_income_expenses_by_statement_month_without_ledger_lines(
    session, test_user, test_workspace, card,
):
    """Spending lands in the statement's (due) month; the previous balance
    and the bill payment are not spending; a refund lowers spending instead
    of being income; a pending statement line is still a charge."""
    last = prev_month(THIS_MONTH)
    due = last.replace(day=5)
    bought = prev_month(last).replace(day=20)  # bought two months ago, billed last month
    await add(session, card, "LOJA", "300", bought, **card_line(due, "purchase"))
    await add(session, card, "PENDENTE", "50", bought, status="pending", **card_line(due, "purchase"))
    await add(session, card, "ESTORNO", "40", bought, "credit", **card_line(due, "credit"))
    await add(session, card, "SALDO ANTERIOR", "999", bought, **card_line(due, "previous_balance"))
    await add(session, card, "PAGAMENTO", "777", bought, "credit", **card_line(due, "payment"))

    report = await report_service.get_income_expenses_report(
        session, test_workspace.id, test_user.id, 3, "monthly", "BRL",
    )
    by_month = {p.date: p.breakdowns for p in report.trend}
    assert by_month[month_label(last)]["expenses"] == pytest.approx(310.0)
    assert by_month[month_label(last)]["income"] == pytest.approx(0.0)
    assert by_month.get(month_label(prev_month(last)), {}).get("expenses", 0) == pytest.approx(0.0)
    composition = {(c.label, c.group): c.value for c in report.composition}
    assert ("Uncategorized", "income") not in composition


@pytest.mark.asyncio
async def test_card_row_billed_later_this_month_is_an_actual(session, test_user, test_workspace, card):
    """A purchase already made whose bill is due later this month is spending
    of this month, not a forecast."""
    if TODAY.day >= 28:
        pytest.skip("needs room for a due date later this month")
    due = TODAY + timedelta(days=1)
    await add(session, card, "LOJA", "120", TODAY - timedelta(days=3), **card_line(due, "purchase"))
    report = await report_service.get_income_expenses_report(
        session, test_workspace.id, test_user.id, 1, "monthly", "BRL",
    )
    totals = {b.key: b.value for b in report.summary.breakdowns}
    assert totals["expenses"] == pytest.approx(120.0)
    assert totals["projectedExpenses"] == pytest.approx(0.0)  # not counted again as a forecast


@pytest.mark.asyncio
async def test_bill_payment_pairs_with_the_bank_debit(session, test_workspace, card, bank):
    d = TODAY - timedelta(days=10)
    payment = await add(session, card, "Pagamento recebido", "382.91", d, "credit", **card_line(d, "payment"))
    echo = await add(session, card, "TRA-1", "382.91", d, "credit", is_ignored=True,
                     ignored_reason="duplicate", **card_line(d, "payment"))
    debit = await add(session, bank, "PAGAMENTO DE FATURA CARTAO", "382.91", d + timedelta(days=1))
    other = await add(session, bank, "TRANSFERENCIA JUAN", "382.91", d)

    # Neither leg is new: nothing happens (a pair the user unlinked stays unlinked).
    assert await pair_card_bill_payments(session, test_workspace.id, candidate_ids=[uuid.uuid4()]) == 0
    assert await pair_card_bill_payments(session, test_workspace.id, candidate_ids=[debit.id]) == 1
    assert payment.transfer_pair_id is not None and payment.transfer_pair_id == debit.transfer_pair_id
    assert echo.transfer_pair_id is None and other.transfer_pair_id is None


@pytest.mark.asyncio
async def test_ambiguous_bill_payment_is_left_alone(session, test_workspace, card, bank):
    d = TODAY - timedelta(days=10)
    payment = await add(session, card, "Pagamento recebido", "550", d, "credit", **card_line(d, "payment"))
    await add(session, bank, "PIX JUAN", "550", d)
    await add(session, bank, "PIX MARIA", "550", d)
    assert await pair_card_bill_payments(session, test_workspace.id) == 0
    assert payment.transfer_pair_id is None


@pytest.mark.asyncio
async def test_cash_flow_takes_pluggy_bills_on_their_due_dates(
    session, test_user, test_workspace, card, bank, monkeypatch,
):
    """Cash only: the starting balance is the bank's; the card's open and
    future bills (with parcels and recurring charges) and an unpaid closed
    one leave on their due dates; a paid bill does not."""
    await add(session, bank, "Saldo inicial", "5000", TODAY, "credit", source="opening_balance")
    await add(session, card, "LOJA", "999", TODAY, **card_line(TODAY + timedelta(days=20), "purchase"))
    due_open = TODAY + timedelta(days=12)
    due_future = TODAY + timedelta(days=42)

    async def fake_statements(_session, account, today=None, projected=None):
        assert account.id == card.id
        return {"statements": [
            {"state": "closed", "status": "paid", "total": D("800"), "paid": D("800"),
             "due_date": TODAY - timedelta(days=20)},
            {"state": "closed", "status": "overdue", "total": D("300"), "paid": D("100"),
             "due_date": TODAY - timedelta(days=2)},
            {"state": "open", "status": "not_closed", "total": D("1452.11"), "paid": D("0"), "due_date": due_open},
            {"state": "future", "status": "not_closed", "total": D("737.77"), "paid": D("0"), "due_date": due_future},
        ]}

    monkeypatch.setattr("app.services.card_statement_service.get_statements", fake_statements)
    report = await report_service.get_cash_flow_report(session, test_workspace.id, test_user.id, months=3, interval="daily")
    totals = {b.key: b.value for b in report.summary.breakdowns}
    assert totals["startingBalance"] == pytest.approx(5000.0)
    assert totals["projectedExpenses"] == pytest.approx(200 + 1452.11 + 737.77)
    by_day = {p.date: p for p in report.trend}
    assert by_day[(TODAY + timedelta(days=1)).isoformat()].breakdowns["outflow"] == pytest.approx(200.0)
    assert by_day[due_open.isoformat()].breakdowns["outflow"] == pytest.approx(1452.11)
    assert by_day[due_future.isoformat()].value == pytest.approx(5000 - 200 - 1452.11 - 737.77)
    card_item = next(c for c in report.composition if c.key == f"card:{card.id}")
    assert card_item.value == pytest.approx(200 + 1452.11 + 737.77)


@pytest.mark.asyncio
async def test_card_recurrences_forecast_by_statement_month(session, test_workspace, card, monkeypatch):
    """P&L projections take a Pluggy card's parcels and recurring charges from
    the statements engine, in the statement's month."""
    from app.models.recurring_transaction import RecurringTransaction
    from app.services.dashboard_service import _get_recurring_projections

    session.add(RecurringTransaction(
        user_id=card.user_id, workspace_id=card.workspace_id, account_id=card.id, description="Streaming",
        amount=D("30"), currency="BRL", type="debit", frequency="monthly", day_of_month=TODAY.day,
        start_date=TODAY, next_occurrence=TODAY, auto_generate=False,
    ))
    await session.commit()
    next_month = (THIS_MONTH + timedelta(days=32)).replace(day=1)

    async def fake_projections(_session, account, *, today):
        return [
            Projection(next_month, D("120"), "installment", {"description": "LOJA", "category_id": None}),
            Projection(next_month, D("30"), "recurrence", {"description": "Streaming", "category_id": None}),
        ]

    monkeypatch.setattr("app.services.projection_service.card_projections", fake_projections)
    month_end = (next_month + timedelta(days=32)).replace(day=1)
    pnl = await _get_recurring_projections(session, test_workspace.id, next_month, month_end)
    assert sorted((p["source"], p["amount"]) for p in pnl) == [("installment", 120.0), ("recurrence", 30.0)]
    # Balance callers keep the recurrence on its own dates.
    balance = await _get_recurring_projections(
        session, test_workspace.id, next_month, month_end, include_transfer_like=True,
    )
    assert [p.get("source") for p in balance] == [None]


@pytest.mark.asyncio
async def test_cash_flow_bills_take_their_known_total(
    session, test_user, test_workspace, card, bank, monkeypatch,
):
    """A bill still to close leaves at its known total (posted rows, parcels
    and recurring charges) — never the average of past bills."""
    await add(session, bank, "Saldo inicial", "9000", TODAY, "credit", source="opening_balance")
    due_open = TODAY + timedelta(days=12)

    async def fake_statements(_session, account, today=None, projected=None):
        closed = [
            {"state": "closed", "status": "paid", "total": D(t), "paid": D(t), "due_date": TODAY - timedelta(days=d)}
            for t, d in (("900", 80), ("1000", 50), ("1100", 20))
        ]
        return {"due_day": due_open.day, "statements": closed + [
            {"state": "open", "status": "not_closed", "total": D("400"), "paid": D("0"), "due_date": due_open},
        ]}

    monkeypatch.setattr("app.services.card_statement_service.get_statements", fake_statements)
    plain = await report_service._card_bill_outflows(session, test_workspace.id, [card], TODAY, TODAY + timedelta(days=75))
    assert [(b.due_date, b.amount) for b in plain] == [(due_open, D("400"))]


def test_card_bill_debt_counts_bills_not_the_used_limit():
    """Net worth counts a card by its bills (user's choice over the bank's
    used limit): closed bills until paid, the open bill as it fills; parcels
    only once their bill closes."""
    today = date(2026, 10, 17)
    debt = CardBillDebt(
        today=today,
        debt_today=D("574.81"),
        rows=[(date(2026, 10, 8), D("1181.37")), (date(2026, 10, 10), D("-100"))],
        cycles=[
            _Cycle(opens=date(2026, 8, 28), closing=date(2026, 9, 28), due=date(2026, 10, 8),
                   known_total=D("1181.37"), real=D(0), state="closed"),
            _Cycle(opens=date(2026, 9, 28), closing=date(2026, 10, 28), due=date(2026, 11, 8),
                   known_total=D("1452.11"), real=D("574.81"), recurring=[(date(2026, 10, 25), D("12.90"))],
                   state="open"),
            _Cycle(opens=date(2026, 10, 28), closing=date(2026, 11, 28), due=date(2026, 12, 8),
                   known_total=D("737.77"), real=D(0)),
        ],
    )
    assert debt.at(today) == pytest.approx(574.81)
    # Back in time: the purchase was not made yet, the payment not done yet.
    assert debt.at(date(2026, 10, 9)) == pytest.approx(474.81)
    assert debt.at(date(2026, 10, 7)) == pytest.approx(1656.18)
    # Ahead: the open bill gains its expected recurring charge...
    assert debt.at(date(2026, 10, 26)) == pytest.approx(587.71)
    # ...is owed in full once closed (parcels included), until its due date.
    assert debt.at(date(2026, 11, 1)) == pytest.approx(1452.11)
    assert debt.at(date(2026, 11, 10)) == pytest.approx(0.0)
    assert debt.at(date(2026, 12, 1)) == pytest.approx(737.77)


def test_window_takes_months_before_and_after_the_reference():
    ref = date(2026, 10, 17)
    assert report_service._window(ref, 6, 0, "daily") == (date(2026, 4, 17), ref)
    assert report_service._window(ref, 0, 3, "daily") == (ref, date(2027, 1, 17))
    assert report_service._window(ref, 3, 6, "daily") == (date(2026, 7, 17), date(2027, 4, 17))
    assert report_service._window(ref, 3, 0, "monthly") == (date(2026, 7, 1), ref)
    assert report_service._window(ref, 3, 12, "monthly") == (date(2026, 7, 1), date(2027, 10, 31))


@pytest.mark.asyncio
async def test_net_worth_counts_card_bills_and_forecasts_ahead(
    session, test_user, test_workspace, card, bank, monkeypatch,
):
    """Net worth takes the card's bills, not its used limit (the bank's
    balance of 4000 here), and points after today forecast them."""
    await add(session, bank, "Saldo inicial", "1000", TODAY, "credit", source="opening_balance")
    card.balance = D("4000")  # used limit, parcels included
    await session.commit()

    async def fake_debt(_session, account, *, today):
        return CardBillDebt(today=today, debt_today=D("300"), cycles=[
            _Cycle(opens=today - timedelta(days=10), closing=today + timedelta(days=20),
                   due=today + timedelta(days=30), known_total=D("400"), real=D("300"), state="open"),
        ])

    monkeypatch.setattr("app.services.card_liability_service.load_card_bill_debt", fake_debt)
    past = await report_service.get_net_worth_report(
        session, test_workspace.id, test_user.id, interval="daily", currency="BRL", past_months=1,
    )
    assert past.trend[-1].value == pytest.approx(700.0)
    assert past.trend[-1].breakdowns["liabilities"] == pytest.approx(300.0)

    ahead = await report_service.get_net_worth_report(
        session, test_workspace.id, test_user.id, interval="daily", currency="BRL", future_months=1,
    )
    assert ahead.meta.reference_date == TODAY.isoformat()
    by_day = {p.date: p for p in ahead.trend}
    closed = by_day[(TODAY + timedelta(days=25)).isoformat()]
    assert closed.breakdowns["liabilities"] == pytest.approx(400.0)  # the bill closed, not yet due
    assert closed.value == pytest.approx(600.0)
    assert "estimate" not in closed.breakdowns


@pytest.mark.asyncio
async def test_income_expenses_window_ahead_counts_every_recurrence_but_skipped(
    session, test_user, test_workspace, bank,
):
    """Ahead, the report assumes every recurrence happens — no historical
    estimate — except the occurrences the user marked as not happening."""
    last = prev_month(THIS_MONTH)
    await add(session, bank, "Mercado", "600", last.replace(day=10))
    months = [add_months(THIS_MONTH, i).replace(day=20) for i in range(1, 4)]
    session.add(RecurringTransaction(
        user_id=test_user.id, workspace_id=test_workspace.id, account_id=bank.id, description="Aluguel",
        amount=D("1000"), currency="BRL", type="debit", frequency="monthly", day_of_month=20,
        start_date=months[0], next_occurrence=months[0], auto_generate=False,
        skipped_periods=[months[1].isoformat()],
    ))
    await session.commit()
    report = await report_service.get_income_expenses_report(
        session, test_workspace.id, test_user.id, interval="monthly", currency="BRL", future_months=3,
    )
    assert report.meta.window_start == THIS_MONTH.isoformat()
    future = {p.date: p.breakdowns for p in report.trend if p.date > month_label(TODAY)}
    assert [future[month_label(m)]["projectedExpenses"] for m in months] == [1000.0, 0.0, 1000.0]
    assert all("estimatedExpenses" not in b for b in future.values())


@pytest.mark.asyncio
async def test_income_expenses_net_counts_the_forecast(
    session, test_user, test_workspace, bank,
):
    """A month's net is what posted plus what is still forecast: a salary
    due later this month already counts, and a month ahead is all forecast."""
    await add(session, bank, "Mercado", "600", THIS_MONTH)
    payday = add_months(THIS_MONTH, 1) - timedelta(days=1)
    session.add(RecurringTransaction(
        user_id=test_user.id, workspace_id=test_workspace.id, account_id=bank.id, description="Salário",
        amount=D("1000"), currency="BRL", type="credit", frequency="monthly", day_of_month=payday.day,
        start_date=payday, next_occurrence=payday, auto_generate=False,
    ))
    await session.commit()
    report = await report_service.get_income_expenses_report(
        session, test_workspace.id, test_user.id, interval="monthly", currency="BRL", future_months=1,
    )
    net = {p.date: p.value for p in report.trend}
    assert net[month_label(TODAY)] == pytest.approx(1000 - 600)
    assert report.summary.primary_value == pytest.approx(sum(net.values()))


@pytest.mark.asyncio
async def test_cash_flow_counts_a_late_occurrence_tomorrow(
    session, test_user, test_workspace, bank, monkeypatch,
):
    """An occurrence expected earlier this month and not seen yet still
    counts (tomorrow) until the user skips it."""
    today = date(2026, 10, 17)
    monkeypatch.setattr(report_service, "app_today", lambda: today)
    monkeypatch.setattr("app.services.recurrence_service.app_today", lambda: today)
    await add(session, bank, "Saldo inicial", "5000", date(2026, 9, 1), "credit", source="opening_balance")
    rec = RecurringTransaction(
        user_id=test_user.id, workspace_id=test_workspace.id, account_id=bank.id, description="Condomínio",
        amount=D("320"), currency="BRL", type="debit", frequency="monthly", day_of_month=5,
        start_date=date(2026, 10, 5), next_occurrence=date(2026, 10, 5), auto_generate=False,
    )
    session.add(rec)
    await session.commit()

    async def balance_tomorrow():
        report = await report_service.get_cash_flow_report(
            session, test_workspace.id, test_user.id, interval="daily", currency="BRL", future_months=1,
        )
        return {p.date: p.value for p in report.trend}[(today + timedelta(days=1)).isoformat()]

    assert await balance_tomorrow() == pytest.approx(5000 - 320)
    rec.skipped_periods = ["2026-10-05"]
    await session.commit()
    assert await balance_tomorrow() == pytest.approx(5000)


@pytest.mark.asyncio
async def test_reports_api_window_params(client, auth_headers):
    empty = await client.get("/api/reports/net-worth", params={"past_months": 0, "future_months": 0}, headers=auth_headers)
    assert empty.status_code == 422
    ok = await client.get(
        "/api/reports/cash-flow", params={"past_months": 3, "future_months": 6, "interval": "monthly"},
        headers=auth_headers,
    )
    assert ok.status_code == 200
    meta = ok.json()["meta"]
    assert meta["window_start"] < meta["reference_date"] < meta["window_end"]


@pytest.mark.asyncio
async def test_net_worth_summary_reads_the_reference_date(session, test_user, test_workspace, bank):
    await add(session, bank, "Saldo inicial", "1000", TODAY - timedelta(days=60), "credit", source="opening_balance")
    await add(session, bank, "Salário", "500", TODAY - timedelta(days=5), "credit")
    report = await report_service.get_net_worth_report(
        session, test_workspace.id, test_user.id, interval="monthly", currency="BRL",
        reference_date=TODAY - timedelta(days=10), past_months=1, future_months=3,
    )
    assert report.summary.primary_value == pytest.approx(1000.0)  # before the salary, whatever comes after
