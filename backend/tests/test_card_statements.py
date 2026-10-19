"""RFC toledo-features Phase 4 — credit-card statements and reference month (AC-4.x)."""
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.credit_card_bill import CreditCardBill
from app.models.transaction import Transaction
from app.services import rule_service
from app.services.card_statement_service import get_statements
from app.services.recompute.billing_cycle import (
    CycleDays,
    KnownBill,
    derive_closing_dates,
    infer_cycle_days,
    month_for_date_in_cycle,
)
from app.services.recompute.months import clamp_day
from app.services.recompute.reference import CardContext, ResolvableRow, calibrate, resolve
from app.services.recompute.statements import Projection, StatementRow, build_statements
from app.services.recompute_service import recompute_account

D = Decimal


def bill(id, due, closing=None, total="0", **kw):
    return KnownBill(id=id, external_id=id, due_date=due, closing_date=closing, total_amount=D(total), **kw)


# --- pure: cycle --------------------------------------------------------------


def test_clamp_day_month_ends():
    assert clamp_day(date(2026, 2, 1), 31) == date(2026, 2, 28)
    assert clamp_day(date(2026, 4, 1), 31) == date(2026, 4, 30)


def test_ac_4_5_cycle_derived_from_bills():
    bills = [bill(f"b{m}", date(2026, m, 10), date(2026, m, 3)) for m in range(4, 10)]
    cycle = infer_cycle_days(bills)
    assert cycle == CycleDays(3, 10)
    # projection beyond the known bills: a purchase on the closing day goes to the next bill
    assert month_for_date_in_cycle(date(2026, 10, 2), [], cycle) == date(2026, 10, 1)
    assert month_for_date_in_cycle(date(2026, 10, 3), [], cycle) == date(2026, 11, 1)


def test_cycle_closing_after_due_day_is_due_next_month():
    cycle = CycleDays(25, 5)
    assert month_for_date_in_cycle(date(2026, 10, 24), [], cycle) == date(2026, 11, 1)
    assert month_for_date_in_cycle(date(2026, 10, 25), [], cycle) == date(2026, 12, 1)
    assert month_for_date_in_cycle(date(2026, 12, 30), [], cycle) == date(2027, 2, 1)


def test_real_bill_boundaries_win_over_projection():
    bills = [bill("a", date(2026, 9, 10), date(2026, 9, 2)), bill("b", date(2026, 10, 10), date(2026, 10, 4))]
    cycle = CycleDays(3, 10)
    assert month_for_date_in_cycle(date(2026, 10, 3), bills, cycle) == date(2026, 10, 1)
    # too far before the first known bill: not trusted, projected instead
    assert month_for_date_in_cycle(date(2026, 7, 1), bills, cycle) == date(2026, 7, 1)


def test_balance_dates_fallback_and_derived_closings():
    assert infer_cycle_days([], date(2026, 10, 25), date(2026, 11, 5)) == CycleDays(25, 5)
    bills = [bill("a", date(2026, 9, 10)), bill("b", date(2026, 10, 10))]
    derive_closing_dates(bills, {"a": [date(2026, 8, 20), date(2026, 8, 30)], "b": [date(2026, 9, 5)]})
    assert bills[0].closing_date == date(2026, 8, 31) and bills[0].closing_estimated
    assert bills[1].closing_date == date(2026, 9, 6)


# --- pure: resolver and calibration -----------------------------------------------


def test_resolver_precedence():
    b = bill("b", date(2026, 11, 10), date(2026, 11, 3))
    ctx = CardContext(bills=[b], cycle=CycleDays(3, 10))
    assert resolve(ResolvableRow(date(2026, 10, 5), effective_bill_date=date(2026, 12, 10), bill=b), ctx).source == "manual"
    assert resolve(ResolvableRow(date(2026, 10, 5), bill=b), ctx) == resolve(ResolvableRow(date(2026, 10, 5), bill=b), ctx)
    assert resolve(ResolvableRow(date(2026, 10, 5), bill=b), ctx).effective_date == date(2026, 11, 10)
    forecast = resolve(ResolvableRow(date(2026, 10, 5), bill_forecast_month=date(2026, 12, 1)), ctx)
    assert (forecast.effective_date, forecast.source) == (date(2026, 12, 10), "forecast")
    cyc = resolve(ResolvableRow(date(2026, 11, 20)), ctx)
    assert (cyc.effective_date, cyc.source) == (date(2026, 12, 10), "cycle")
    assert resolve(ResolvableRow(date(2026, 11, 20)), CardContext(bills=[], cycle=None)).source == "tx_date"
    assert resolve(ResolvableRow(date(2026, 11, 20)), None).source == "tx_date"


def test_ac_4_3_calibration_learns_forecast_offset():
    bills = [bill(f"b{m}", date(2026, m, 10), date(2026, m, 3)) for m in range(3, 11)]
    by_month = {b.month: b for b in bills}
    rows = [
        ResolvableRow(date(2026, m - 1, 15), bill=by_month[date(2026, m, 1)],
                      bill_forecast_month=date(2026, m - 1, 1))
        for m in range(3, 11) for _ in range(3)
    ]
    stats = calibrate(rows, CardContext(bills=bills, cycle=CycleDays(3, 10)))
    assert stats["forecast_offset_months"] == 1
    assert stats["truth_samples"] == 24


def test_ac_4_4_fallback_order_follows_accuracy():
    bills = [bill(f"b{m}", date(2026, m, 10), date(2026, m, 3)) for m in range(1, 13)]
    by_month = {b.month: b for b in bills}
    rows = []
    for i in range(20):
        m = 2 + i % 10
        truth = by_month[date(2026, m, 1)]
        rows.append(ResolvableRow(
            date(2026, m - 1, 25),  # cycle says month m (closing 3 of m) ... after the 3rd → m
            bill=truth,
            bill_post_date=date(2026, m - 1, 20) if i < 19 else date(2026, m, 20),
            bill_forecast_month=date(2026, m, 1) if i < 12 else date(2026, m - 1, 1),
        ))
    stats = calibrate(rows, CardContext(bills=bills, cycle=CycleDays(3, 10)))
    assert stats["methods"]["post_date"] == {"samples": 20, "hits": 19}
    assert stats["fallback_order"][0] in ("post_date", "cycle")
    assert stats["fallback_order"].index("post_date") < stats["fallback_order"].index("forecast")


def test_calibration_prior_without_samples():
    stats = calibrate([], CardContext(bills=[], cycle=None))
    assert stats["fallback_order"] == ["forecast", "post_date", "cycle"]


# --- pure: statements -------------------------------------------------------------


def row(id, d, month, amount, kind, **kw):
    return StatementRow(id=id, date=d, month=month, signed_amount=D(amount), kind=kind, **kw)


def test_ac_4_7_states():
    today = date(2026, 10, 17)
    cycle = CycleDays(25, 5)
    bills = [bill("oct", date(2026, 10, 5), date(2026, 9, 25), "100")]
    rows = [
        row("a", date(2026, 9, 20), date(2026, 10, 1), "-100", "purchase"),
        row("b", date(2026, 10, 10), date(2026, 11, 1), "-50", "purchase"),
    ]
    out = {s.month: s for s in build_statements(
        bills=bills, rows=rows, cycle=cycle, today=today,
        projected=[Projection(date(2026, 12, 1), D("30"))],
    )}
    assert out[date(2026, 10, 1)].state == "closed"
    assert out[date(2026, 11, 1)].state == "open"
    assert out[date(2026, 12, 1)].state == "future"
    assert out[date(2026, 12, 1)].projected_installments == D("30.00")
    assert out[date(2026, 12, 1)].total == D("30.00")

    # closing day 3: the cycle closed on 2026-10-03, no bill returned yet
    out = {s.month: s for s in build_statements(
        bills=[], rows=[row("c", date(2026, 9, 20), date(2026, 10, 1), "-80", "purchase")],
        cycle=CycleDays(3, 10), today=today,
    )}
    st = out[date(2026, 10, 1)]
    assert st.state == "closed_estimated" and st.status == "overdue" and st.total == D("80.00")


def test_ac_4_8_payment_allocation_and_prepayment():
    cycle = CycleDays(3, 10)
    bills = [
        bill("sep", date(2026, 9, 10), date(2026, 9, 3), "1000"),
        bill("oct", date(2026, 10, 10), date(2026, 10, 3), "300"),
    ]
    rows = [
        row("p1", date(2026, 8, 20), date(2026, 9, 1), "-1000", "purchase"),
        row("pay", date(2026, 9, 8), date(2026, 10, 1), "1200", "payment"),
        row("p2", date(2026, 9, 15), date(2026, 10, 1), "-500", "purchase"),
    ]
    out = {s.month: s for s in build_statements(bills=bills, rows=rows, cycle=cycle, today=date(2026, 10, 17))}
    sep, oct_ = out[date(2026, 9, 1)], out[date(2026, 10, 1)]
    assert (sep.status, sep.paid_at, sep.paid) == ("paid", date(2026, 9, 8), D("1000.00"))
    assert oct_.prepaid == D("200.00")
    assert oct_.reconciliation == {"computed": D("300.00"), "diff": D("0.00")}


def test_ac_4_9_ledger_bank_total():
    rows = [
        row("prev", date(2026, 9, 4), date(2026, 10, 1), "-500", "previous_balance"),
        row("buy", date(2026, 9, 10), date(2026, 10, 1), "-300", "purchase"),
        row("pay", date(2026, 9, 12), date(2026, 10, 1), "500", "payment"),
    ]
    out = build_statements(bills=[bill("oct", date(2026, 10, 10), date(2026, 10, 3), "300")],
                           rows=rows, cycle=CycleDays(3, 10), today=date(2026, 10, 17))
    assert out[0].reconciliation == {"computed": D("300.00"), "diff": D("0.00")}
    assert out[0].spent == D("300.00")


def test_settlement_cross_check_marks_paid_without_payment_rows():
    bills = [
        bill("sep", date(2026, 9, 10), date(2026, 9, 3), "400"),
        bill("oct", date(2026, 10, 10), date(2026, 10, 3), "100", finance_charges=D("5"),
             payments=[{"amount": 405, "paymentDate": "2026-09-09"}]),
    ]
    rows = [row("p", date(2026, 8, 20), date(2026, 9, 1), "-400", "purchase")]
    out = {s.month: s for s in build_statements(bills=bills, rows=rows, cycle=CycleDays(3, 10), today=date(2026, 10, 17))}
    assert out[date(2026, 9, 1)].status == "paid"
    assert out[date(2026, 9, 1)].paid_at == date(2026, 9, 9)


def test_ac_4_12_unknown_before_history():
    bills = [bill("jul", date(2026, 7, 10), date(2026, 7, 3), "250"), bill("aug", date(2026, 8, 10), date(2026, 8, 3), "90")]
    rows = [row("p", date(2026, 7, 20), date(2026, 8, 1), "-90", "purchase")]
    out = {s.month: s for s in build_statements(bills=bills, rows=rows, cycle=CycleDays(3, 10), today=date(2026, 10, 17))}
    assert (out[date(2026, 7, 1)].status, out[date(2026, 7, 1)].history) == ("unknown", "before")
    assert out[date(2026, 8, 1)].history == "first"


# --- service ----------------------------------------------------------------------


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


async def add_bill(session, account, ext, due, total, closing=None, **kw) -> CreditCardBill:
    b = CreditCardBill(user_id=account.user_id, workspace_id=account.workspace_id, account_id=account.id,
                       external_id=ext, due_date=due, total_amount=D(total), closing_date=closing, **kw)
    session.add(b)
    await session.commit()
    return b


async def add_tx(session, account, desc, amount, d, type_="debit", **kw) -> Transaction:
    tx = Transaction(user_id=account.user_id, workspace_id=account.workspace_id, account_id=account.id,
                     description=desc, original_description=desc, amount=D(amount), currency="BRL",
                     date=d, type=type_, source="sync", external_id=kw.pop("external_id", desc + str(d)), **kw)
    session.add(tx)
    await session.commit()
    return tx


@pytest.mark.asyncio
async def test_ac_4_1_and_4_2_bill_and_manual_override(session, card, client: AsyncClient, auth_headers):
    await add_bill(session, card, "B11", date(2026, 11, 10), "50", date(2026, 11, 3))
    tx = await add_tx(session, card, "LOJA", "50", date(2026, 10, 20), provider_bill_external_id="B11")
    await recompute_account(session, card.id)
    await session.refresh(tx)
    assert (tx.effective_date, tx.effective_date_source) == (date(2026, 11, 10), "bill")

    resp = await client.patch(f"/api/transactions/{tx.id}", json={"effective_bill_date": "2026-12-10"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    await recompute_account(session, card.id)
    await session.refresh(tx)
    assert (tx.effective_date, tx.effective_date_source) == (date(2026, 12, 10), "manual")


@pytest.mark.asyncio
async def test_ac_4_3_open_bill_purchase_uses_calibrated_forecast(session, card):
    for m in range(4, 11):
        await add_bill(session, card, f"B{m}", date(2026, m, 10), "30", date(2026, m, 3))
    for m in range(5, 11):
        for i in range(4):
            await add_tx(session, card, f"P{m}-{i}", "10", date(2026, m - 1, 10 + i),
                         provider_bill_external_id=f"B{m}", bill_forecast_month=date(2026, m - 1, 1))
    pending = await add_tx(session, card, "OPEN", "20", date(2026, 10, 12), status="pending",
                           bill_forecast_month=date(2026, 10, 1))
    await recompute_account(session, card.id)
    await session.refresh(pending)
    await session.refresh(card)
    assert card.forecast_offset_months == 1
    assert (pending.effective_date, pending.effective_date_source) == (date(2026, 11, 10), "forecast")
    assert card.resolution_stats["truth_samples"] == 24


@pytest.mark.asyncio
async def test_ac_4_5_and_4_6_cycle_days_derived_then_overridden(session, card, client: AsyncClient, auth_headers):
    for m in range(4, 10):
        await add_bill(session, card, f"B{m}", date(2026, m, 10), "1", date(2026, m, 3))
    on_third = await add_tx(session, card, "ON 3RD", "5", date(2026, 10, 3))
    await recompute_account(session, card.id)
    await session.refresh(card)
    await session.refresh(on_third)
    assert (card.statement_close_day, card.payment_due_day, card.cycle_days_manual) == (3, 10, False)
    assert on_third.effective_date == date(2026, 11, 10)

    resp = await client.patch(f"/api/accounts/{card.id}", json={"statement_close_day": 28, "payment_due_day": 8},
                              headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["cycle_days_manual"] is True
    await recompute_account(session, card.id)
    await session.refresh(card)
    assert (card.statement_close_day, card.payment_due_day) == (28, 8)

    resp = await client.patch(f"/api/accounts/{card.id}", json={"statement_close_day": None, "payment_due_day": None},
                              headers=auth_headers)
    assert resp.status_code == 200
    await session.refresh(card)
    assert (card.statement_close_day, card.payment_due_day, card.cycle_days_manual) == (3, 10, False)


async def _caixa(session, card, test_user, test_workspace, with_echo=False, carried=False):
    await rule_service.install_builtin_pack(session, test_workspace.id, test_user.id, "BR_CARDS")
    await add_bill(session, card, "SEP", date(2026, 9, 10), "540", date(2026, 9, 3))
    await add_bill(session, card, "OCT", date(2026, 10, 10), "300", date(2026, 10, 3))
    # September statement
    await add_tx(session, card, "COMPRA SET", "580", date(2026, 8, 20), provider_bill_external_id="SEP")
    await add_tx(session, card, "ESTORNO LOJA", "40", date(2026, 8, 12), type_="credit", provider_bill_external_id="SEP")
    # October statement (ledger)
    await add_tx(session, card, "TOTAL DA FATURA ANTERIOR", "500", date(2026, 9, 4), provider_bill_external_id="OCT")
    await add_tx(session, card, "LOJA OUT", "300", date(2026, 9, 10), provider_bill_external_id="OCT")
    await add_tx(session, card, "PGTO.BOLETO", "500", date(2026, 9, 9), type_="credit", provider_bill_external_id="OCT")
    if carried:
        await add_tx(session, card, "AJUSTE CREDITO", "40", date(2026, 8, 12), type_="credit",
                     provider_bill_external_id="OCT", external_id="carried")
    if with_echo:
        await add_tx(session, card, "Pagamento recebido", "500", date(2026, 9, 9), type_="credit",
                     operation_type="PAGAMENTO_FATURA", provider_bill_external_id="OCT", external_id="echo")
    await recompute_account(session, card.id)


@pytest.mark.asyncio
async def test_ac_4_9_and_4_10_caixa_ledger_and_payment_echo(session, card, test_user, test_workspace):
    await _caixa(session, card, test_user, test_workspace, with_echo=True)
    view = await get_statements(session, card, today=date(2026, 10, 17), projected=[])
    oct_ = next(s for s in view["statements"] if s["month"] == date(2026, 10, 1))
    assert oct_["reconciliation"] == {"computed": D("300.00"), "diff": D("0.00")}
    from sqlalchemy import select

    echo = (await session.execute(select(Transaction).where(Transaction.external_id == "echo"))).scalar_one()
    payment = (await session.execute(select(Transaction).where(Transaction.description == "PGTO.BOLETO"))).scalar_one()
    assert (echo.is_ignored, echo.ignored_reason, echo.duplicate_of_id) == (True, "duplicate", payment.id)


@pytest.mark.asyncio
async def test_ac_4_11_carried_over_credit(session, card, test_user, test_workspace):
    await _caixa(session, card, test_user, test_workspace, carried=True)
    from sqlalchemy import select

    copy = (await session.execute(select(Transaction).where(Transaction.external_id == "carried"))).scalar_one()
    assert (copy.card_line_kind, copy.card_line_kind_source, copy.card_line_kind_reason) == (
        "previous_balance", "default", "carried_over")
    view = await get_statements(session, card, today=date(2026, 10, 17), projected=[])
    oct_ = next(s for s in view["statements"] if s["month"] == date(2026, 10, 1))
    assert oct_["spent"] == D("300.00")  # the repeated credit does not reduce October's spending


@pytest.mark.asyncio
async def test_statements_endpoint(session, card, client: AsyncClient, auth_headers):
    await add_bill(session, card, "B10", date(2026, 10, 10), "50", date(2026, 10, 3))
    await add_tx(session, card, "LOJA", "50", date(2026, 9, 20), provider_bill_external_id="B10")
    await recompute_account(session, card.id)
    await session.commit()
    resp = await client.get(f"/api/accounts/{card.id}/statements", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["statements"][0]["month"] == "2026-10-01"
    assert body["statements"][0]["state"] == "closed"


@pytest.mark.asyncio
async def test_ac_4_13_reports_follow_effective_dates(session, card, test_user, test_workspace):
    from app.services import admin_service
    from app.services.dashboard_service import get_spending_by_category

    await admin_service.set_app_setting(session, "credit_card_accounting_mode", "accrual")
    # Past months: the dashboard never counts what is after today.
    await add_bill(session, card, "B9", date(2026, 9, 10), "70", date(2026, 9, 3))
    await add_tx(session, card, "LOJA A", "70", date(2026, 8, 20), provider_bill_external_id="B9",
                 amount_primary=D("70"))
    await recompute_account(session, card.id)
    await session.commit()
    september = await get_spending_by_category(session, test_workspace.id, test_user.id, month=date(2026, 9, 1))
    august = await get_spending_by_category(session, test_workspace.id, test_user.id, month=date(2026, 8, 1))
    assert sum(Decimal(str(c.total)) for c in september) == D("70")
    assert sum(Decimal(str(c.total)) for c in august) == 0
    view = await get_statements(session, card, today=date(2026, 10, 17), projected=[])
    assert next(s for s in view["statements"] if s["month"] == date(2026, 9, 1))["spent"] == D("70.00")
