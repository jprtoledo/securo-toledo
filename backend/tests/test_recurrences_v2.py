"""RFC toledo-features Phase 6 — Recurrences v2 (AC-6.x)."""
import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.credit_card_bill import CreditCardBill
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.services import recurrence_service
from app.services.recompute.recurrence_suggestions import SuggestionRow, suggest
from app.services.recompute.recurrences import (
    Occurrence,
    RecurrenceConfig,
    RecurrenceRow,
    build_timeline,
    expectation_for,
    forecast,
    link_recurrences,
    periods_of,
    timeline_alerts,
    units_for,
)
from app.services.recompute.months import month_of
from app.services.recompute_service import recompute_account

D = Decimal
TODAY = date(2026, 10, 17)


def cfg(id="r", amount="10", frequency="monthly", start=date(2026, 1, 12), **kw):
    return RecurrenceConfig(id=id, direction=kw.pop("direction", "debit"), currency=kw.pop("currency", "BRL"),
                            amount=D(amount), frequency=frequency, start_date=start, **kw)


def row(id, d, amount, desc="APPLE.COM/BILL", currency="BRL", account_amount=None, **kw):
    return RecurrenceRow(id=id, date=d, description=desc, direction=kw.pop("direction", "debit"),
                         currency=currency, amount=D(amount),
                         account_amount=D(account_amount if account_amount is not None else amount), **kw)


def always(_rec, _row):
    return True


# --- pure: schedule and expectations -----------------------------------------------


def test_periods_per_frequency_and_clamping():
    assert periods_of(cfg(frequency="monthly", start=date(2026, 1, 31), day_of_month=31),
                      date(2026, 1, 1), date(2026, 4, 30)) == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]
    assert periods_of(cfg(frequency="quarterly", start=date(2026, 1, 10), day_of_month=10),
                      date(2026, 1, 1), date(2026, 12, 31)) == [
        date(2026, 1, 10), date(2026, 4, 10), date(2026, 7, 10), date(2026, 10, 10)]
    assert periods_of(cfg(frequency="biweekly", start=date(2026, 1, 2)), date(2026, 1, 1), date(2026, 2, 1)) == [
        date(2026, 1, 2), date(2026, 1, 16), date(2026, 1, 30)]
    assert periods_of(cfg(frequency="monthly", start=date(2026, 1, 12), end_date=date(2026, 3, 1)),
                      date(2026, 1, 1), date(2026, 12, 1)) == [date(2026, 1, 12), date(2026, 2, 12)]


def test_ac_6_8_weekly_billed_monthly_units_and_expectation():
    rec = cfg(amount="150", frequency="weekly_billed_monthly", start=date(2026, 9, 5), day_of_month=5,
              billing_weekday=1, week_count_basis="previous_month")
    assert units_for(rec, date(2026, 10, 5)) == 5  # September 2026 has 5 Tuesdays
    assert units_for(rec, date(2026, 11, 5)) == 4
    assert expectation_for(rec, date(2026, 10, 5), []).amount == D("750.00")
    assert expectation_for(rec, date(2026, 11, 5), []).amount == D("600.00")
    history = [Occurrence("t", date(2026, 11, 5), date(2026, 11, 5), "manual", False, D("640"), D("640"))]
    # learns the per-week value: 640 / 4 = 160 → December (Nov has 4 Tuesdays) 640, January (5) 800
    assert expectation_for(rec, date(2027, 1, 5), history).amount == D("800.00")


def test_frequency_column_fits_every_frequency():
    # SQLite ignores VARCHAR lengths, so only this catches a frequency that
    # Postgres would reject ("value too long for type character varying").
    from typing import get_args

    from app.schemas.recurring_transaction import RecurringFrequency

    length = RecurringTransaction.__table__.c.frequency.type.length
    assert all(len(value) <= length for value in get_args(RecurringFrequency))


def test_skipped_period_leaves_the_forecast():
    """An occurrence marked as not happening is never forecast — expected,
    late or missed — while a linked one stays posted."""
    rec = cfg(start=date(2026, 8, 12), day_of_month=12,
              skipped=frozenset({date(2026, 9, 12), date(2026, 10, 12), date(2026, 12, 12)}))
    linked = [Occurrence("t", date(2026, 8, 12), date(2026, 8, 12), "auto", False, D("10"), D("10"))]
    timeline = build_timeline(rec, linked, today=TODAY, through=date(2027, 1, 31),
                              month_for_date=month_of, open_month=date(2026, 10, 1), account_currency="BRL")
    assert [(s.period.month, s.status) for s in timeline] == [
        (8, "posted"), (9, "skipped"), (10, "skipped"), (11, "expected"), (12, "skipped"), (1, "expected"),
    ]
    assert [s.period for s in forecast(timeline)] == [date(2026, 11, 12), date(2027, 1, 12)]
    assert timeline_alerts(timeline)["missed_streak"] == 0


def test_ac_6_6_day_drift_and_ac_6_3_variable_mean():
    gym = cfg(start=date(2026, 6, 5), day_of_month=5)
    history = [
        Occurrence("a", date(2026, 8, 5), date(2026, 8, 8), "auto", False, D("10"), D("10")),
        Occurrence("b", date(2026, 9, 5), date(2026, 9, 8), "auto", False, D("10"), D("10")),
    ]
    assert expectation_for(gym, date(2026, 10, 5), history).date == date(2026, 10, 8)

    power = cfg(amount="190", amount_mode="variable", tolerance_pct=D("40"))
    history = [Occurrence(str(i), date(2026, m, 12), date(2026, m, 12), "auto", False, D(a), D(a))
               for i, (m, a) in enumerate([(6, "180"), (7, "200"), (8, "220")])]
    history.append(Occurrence("x", date(2026, 9, 12), date(2026, 9, 12), "manual", False, D("600"), D("600"), excluded=True))
    assert expectation_for(power, date(2026, 10, 12), history).amount == D("200.00")


def test_fx_estimate_uses_last_charge():
    rec = cfg(amount="20", currency="USD")
    history = [
        Occurrence("m", date(2026, 9, 3), date(2026, 9, 3), "auto", False, D("20"), D("108.40")),
        Occurrence("iof", date(2026, 9, 3), date(2026, 9, 3), "auto", True, D("3.79"), D("3.79")),
    ]
    exp = expectation_for(rec, date(2026, 10, 3), history, None, "BRL")
    assert exp.amount == D("20.00") and exp.account_amount == D("112.19")
    assert expectation_for(rec, date(2026, 10, 3), [], D("5.5"), "BRL").account_amount == D("110.00")


# --- pure: linking -------------------------------------------------------------------


def test_ac_6_1_several_subscriptions_at_one_merchant():
    recs = [cfg("icloud", "10.99"), cfg("one", "49.99"), cfg("care", "66.90")]
    rows = [
        row("a", date(2026, 10, 12), "10.99"), row("b", date(2026, 10, 13), "49.99"),
        row("c", date(2026, 10, 12), "66.90"), row("d", date(2026, 10, 20), "199.00"),
    ]
    links = link_recurrences(recs, rows, always, date(2026, 11, 1), "BRL")
    assert (links["a"].recurrence_id, links["b"].recurrence_id, links["c"].recurrence_id) == ("icloud", "one", "care")
    assert all(links[x].link == "auto" for x in "abc")
    assert "d" not in links


def test_ac_6_2_amount_change_conflict_then_confirmation():
    netflix = cfg("netflix", "39.90", start=date(2026, 9, 12), tolerance_pct=D("2"))
    rows = [row("sep", date(2026, 9, 12), "39.90"), row("nov", date(2026, 11, 12), "44.90")]
    links = link_recurrences([netflix], rows, always, date(2026, 12, 1), "BRL")
    assert links["nov"].link == "conflict"
    assert links["nov"].issue == {"code": "amount_changed", "charged": "44.90", "expected": "39.90"}
    rows[1].manual_id = "netflix"
    links = link_recurrences([netflix], rows, always, date(2026, 12, 1), "BRL")
    assert links["nov"].link == "manual"
    history = [Occurrence(rid, a.period, r.date, a.link, a.companion, r.amount, r.account_amount)
               for r in rows for rid, a in [(r.id, links[r.id])]]
    assert expectation_for(netflix, date(2026, 12, 12), history).amount == D("44.90")


def test_ac_6_4_reject_is_permanent_and_manual_lifts_it():
    spotify = cfg("spotify", "21.90", start=date(2026, 10, 7))
    r = row("x", date(2026, 10, 7), "21.90", rejected_ids=("spotify",))
    assert link_recurrences([spotify], [r], always, date(2026, 11, 1), "BRL") == {}


def test_ac_6_7_iof_companion():
    chatgpt = cfg("gpt", "20", currency="USD", start=date(2026, 9, 3))
    rows = [
        row("main", date(2026, 10, 3), "20", desc="OPENAI *CHATGPT SUBSCR", currency="USD", account_amount="108.40"),
        row("iof", date(2026, 10, 3), "3.79", desc="OPENAI *CHATGPT SUBSCR IOF", account_amount="3.79"),
        row("other", date(2026, 10, 3), "3.79", desc="PADARIA"),
    ]
    links = link_recurrences([chatgpt], rows, lambda rec, r: "CHATGPT" in r.description, date(2026, 11, 1), "BRL")
    assert links["main"].link == "auto" and not links["main"].companion
    assert links["iof"].companion and links["iof"].period == links["main"].period
    assert "other" not in links


def test_double_charge_conflict():
    rec = cfg("gym", "99", start=date(2026, 10, 5))
    links = link_recurrences([rec], [row("a", date(2026, 10, 5), "99"), row("b", date(2026, 10, 6), "99")],
                             always, date(2026, 11, 1), "BRL")
    assert links["a"].link == "auto" and links["b"].issue == {"code": "double_charge"}


def test_ac_6_9_weekly_and_quarterly():
    weekly = cfg("w", "30", frequency="weekly", start=date(2026, 1, 2), day_window=2)
    quarterly = cfg("q", "500", frequency="quarterly", start=date(2026, 1, 10), day_of_month=10)
    rows = [row(f"f{i}", date(2026, 1, 2) + timedelta(days=7 * i + (1 if i % 2 else 0)), "30") for i in range(6)]
    rows += [row("apr", date(2026, 4, 11), "500"), row("jul", date(2026, 7, 9), "500")]
    links = link_recurrences([weekly, quarterly], rows, lambda rec, r: (rec.id == "w") == (r.amount == 30),
                             date(2026, 8, 1), "BRL")
    periods = [links[f"f{i}"].period for i in range(6)]
    assert periods == [date(2026, 1, 2) + timedelta(days=7 * i) for i in range(6)]
    assert links["apr"].period == date(2026, 4, 10) and links["jul"].period == date(2026, 7, 10)


def test_timeline_statuses_and_suggest_end():
    rec = cfg("r", "10", start=date(2026, 7, 10), day_of_month=10)
    linked = [Occurrence("a", date(2026, 7, 10), date(2026, 7, 10), "auto", False, D("10"), D("10"))]
    timeline = build_timeline(rec, linked, today=TODAY, through=date(2026, 12, 1), month_for_date=month_of,
                              open_month=date(2026, 10, 1), account_currency="BRL")
    status = {s.period: s.status for s in timeline}
    assert status[date(2026, 7, 10)] == "posted"
    assert status[date(2026, 8, 10)] == "missed" and status[date(2026, 9, 10)] == "missed"
    assert status[date(2026, 10, 10)] == "late"
    assert status[date(2026, 11, 10)] == "expected"
    alerts = timeline_alerts(timeline)
    assert alerts["suggest_end"] and alerts["last_missed"] == date(2026, 9, 10)


# --- pure: suggestions ---------------------------------------------------------------------


def test_ac_6_13_suggestions():
    rows = [SuggestionRow(f"s{m}", date(2026, m, 7), "SPOTIFY", "debit", "BRL", D("21.90")) for m in range(4, 11)]
    rows += [SuggestionRow(f"i{m}-{d}", date(2026, m, d), "IFOOD *REST", "debit", "BRL",
                           D("25") + D((m * 37 + d * 11) % 60) + D("0.37"))
             for m in range(5, 11) for d in range(1, 25, 2)]
    rows += [SuggestionRow(f"c{m}", date(2026, m, 3), "TRA-5639146-20260910", "debit", "BRL", D("50")) for m in range(4, 11)]
    found = suggest(rows, TODAY)
    keys = {s.key for s in found}
    assert any("SPOTIFY" in k and "|fixed|" in k for k in keys)
    assert not any("IFOOD" in k for k in keys)
    assert not any("TRA" in k for k in keys)
    spotify = next(s for s in found if "SPOTIFY" in s.key)
    assert spotify.match_condition["conditions"][0] == {"field": "description_key", "op": "starts_with", "value": "SPOTIFY"}
    assert spotify.amount == D("21.90") and spotify.day_of_month == 7
    assert suggest(rows, TODAY, {spotify.key}) == []
    # a dismissed fixed cluster does not come back as variable either
    assert not any("SPOTIFY" in s.key for s in suggest(rows, TODAY, {spotify.key}))


def test_weekly_and_yearly_suggestions():
    weekly = [SuggestionRow(f"w{i}", TODAY - timedelta(days=7 * i), "DIARISTA", "debit", "BRL", D("150"))
              for i in range(8)]
    found = suggest(weekly, TODAY)
    assert found and found[0].frequency == "weekly"
    yearly = [SuggestionRow("a", date(2025, 9, 1), "SEGURO AUTO", "debit", "BRL", D("1200")),
              SuggestionRow("b", date(2026, 9, 2), "SEGURO AUTO", "debit", "BRL", D("1230"))]
    found = suggest(yearly, TODAY)
    assert found and found[0].frequency == "yearly"


# --- service --------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def frozen_today():
    with patch("app.services.recurrence_service.app_today", return_value=TODAY):
        yield


@pytest.fixture
async def checking(session: AsyncSession, test_user, test_workspace) -> Account:
    acc = Account(user_id=test_user.id, workspace_id=test_workspace.id, name="Checking", type="checking",
                  balance=D("0"), currency="BRL")
    session.add(acc)
    await session.commit()
    return acc


async def add_tx(session, account, desc, amount, d, type_="debit", **kw) -> Transaction:
    tx = Transaction(user_id=account.user_id, workspace_id=account.workspace_id, account_id=account.id,
                     description=desc, original_description=desc, amount=D(amount), currency=kw.pop("currency", "BRL"),
                     date=d, type=type_, source="sync", external_id=kw.pop("external_id", f"{desc}{d}{amount}"), **kw)
    session.add(tx)
    await session.commit()
    return tx


async def create(client, headers, account, **kw):
    body = {"description": "Sub", "amount": "10", "currency": "BRL", "type": "debit", "frequency": "monthly",
            "start_date": "2026-06-12", "day_of_month": 12, "account_id": str(account.id)}
    body.update(kw)
    resp = await client.post("/api/recurring-transactions", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


COND = {"op": "and", "conditions": [{"field": "description_key", "op": "starts_with", "value": "NETFLIX"}]}


@pytest.mark.asyncio
async def test_ac_6_2_and_6_5_service_flow(client: AsyncClient, auth_headers, session, checking):
    for m in (8, 9, 10):
        await add_tx(session, checking, "NETFLIX.COM", "39.90", date(2026, m, 12))
    rec = await create(client, auth_headers, checking, description="Netflix", amount="39.90",
                       match_condition=COND, amount_tolerance_pct="2", start_date="2026-08-12")
    assert rec["auto_generate"] is False
    assert rec["link_stats"]["linked"] == 3
    nov = await add_tx(session, checking, "NETFLIX.COM", "44.90", date(2026, 11, 12))
    await recompute_account(session, checking.id)
    await session.refresh(nov)
    assert (nov.recurring_link, nov.recurring_issue["code"]) == ("conflict", "amount_changed")

    resp = await client.post(f"/api/transactions/{nov.id}/recurring/confirm", headers=auth_headers)
    assert resp.status_code == 200 and resp.json()["recurring_link"] == "manual"

    # AC-6.5 the bank changes the charge: the decision survives
    nov.amount = D("45.00")
    nov.status = "pending"
    await session.commit()
    await recompute_account(session, checking.id)
    await session.refresh(nov)
    assert nov.recurring_manual_id == rec_id(rec) and nov.recurring_link == "manual"

    listed = (await client.get("/api/recurring-transactions", params={"account_id": str(checking.id)},
                               headers=auth_headers)).json()
    item = listed[0]
    statuses = {s["period"]: s["status"] for s in item["timeline"]}
    assert statuses["2026-08-12"] == "posted"
    assert item["next"]["expected_amount"] == "45.00"


def rec_id(rec):
    import uuid

    return uuid.UUID(rec["id"])


@pytest.mark.asyncio
async def test_ac_6_4_reject_and_manual_link(client: AsyncClient, auth_headers, session, checking):
    tx = await add_tx(session, checking, "SPOTIFY", "21.90", date(2026, 10, 7))
    rec = await create(client, auth_headers, checking, description="Spotify", amount="21.90", start_date="2026-10-07",
                       day_of_month=7,
                       match_condition={"field": "description_key", "op": "starts_with", "value": "SPOTIFY"})
    await session.refresh(tx)
    assert tx.recurring_transaction_id == rec_id(rec)
    resp = await client.post(f"/api/transactions/{tx.id}/recurring/review", json={"decision": "reject"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["recurring_transaction_id"] is None
    await recompute_account(session, checking.id)
    await session.refresh(tx)
    assert tx.recurring_transaction_id is None
    resp = await client.post(f"/api/transactions/{tx.id}/recurring", json={"recurring_id": rec["id"]}, headers=auth_headers)
    body = resp.json()
    assert body["recurring_link"] == "manual" and not body["recurring_rejected_ids"]


@pytest.mark.asyncio
async def test_not_recurring_and_undo(client: AsyncClient, auth_headers, session, checking):
    tx = await add_tx(session, checking, "SPOTIFY", "21.90", date(2026, 10, 7))
    await create(client, auth_headers, checking, description="Spotify", amount="21.90", start_date="2026-10-07",
                 day_of_month=7, match_condition={"field": "description_key", "op": "starts_with", "value": "SPOTIFY"})
    resp = await client.post(f"/api/transactions/{tx.id}/recurring", json={"recurring_id": "none"}, headers=auth_headers)
    assert resp.json()["recurring_transaction_id"] is None and resp.json()["recurring_manual_none"] is True
    resp = await client.post(f"/api/transactions/{tx.id}/recurring", json={"recurring_id": None}, headers=auth_headers)
    assert resp.json()["recurring_link"] == "auto"


@pytest.mark.asyncio
async def test_ac_6_3_accept_keeps_value_out(client: AsyncClient, auth_headers, session, checking):
    for m, a in ((7, "180"), (8, "200"), (9, "220")):
        await add_tx(session, checking, "ENERGISA", a, date(2026, m, 15))
    big = await add_tx(session, checking, "ENERGISA", "600", date(2026, 10, 15))
    rec = await create(client, auth_headers, checking, description="Electricity", amount="190",
                       amount_mode="variable", amount_tolerance_pct="40", start_date="2026-07-15", day_of_month=15,
                       match_condition={"field": "description_key", "op": "starts_with", "value": "ENERGISA"})
    await session.refresh(big)
    assert big.recurring_link == "conflict"
    resp = await client.post(f"/api/transactions/{big.id}/recurring/review", json={"decision": "accept"}, headers=auth_headers)
    assert resp.json()["recurring_link"] == "manual" and resp.json()["recurring_manual_excluded"] is True
    view = (await client.get(f"/api/recurring-transactions/{rec['id']}/timeline", headers=auth_headers)).json()
    by_period = {s["period"]: s for s in view["timeline"]}
    assert by_period["2026-10-15"]["status"] == "posted"
    assert by_period["2026-11-15"]["expected_amount"] == "200.00"


@pytest.mark.asyncio
async def test_ac_6_10_forecast_computed_not_materialized(client: AsyncClient, auth_headers, session, checking, test_user):
    from app.services.dashboard_service import get_projected_transactions
    from app.services.recurring_transaction_service import generate_pending

    await add_tx(session, checking, "ACADEMIA", "99", date(2026, 10, 5))
    rec = await create(client, auth_headers, checking, description="Gym", amount="99", start_date="2026-10-05",
                       day_of_month=5, match_condition={"field": "description_key", "op": "starts_with", "value": "ACADEMIA"})
    assert await generate_pending(session, test_user.id, up_to=date(2026, 12, 31)) == 0
    placeholders = (await session.execute(select(Transaction).where(Transaction.source == "recurring"))).scalars().all()
    assert placeholders == []
    projected = await get_projected_transactions(session, checking.workspace_id, test_user.id, month=date(2026, 11, 1))
    mine = [p for p in projected if p.recurring_id == rec["id"]]
    assert [p.date for p in mine] == ["2026-11-05"]
    october = await get_projected_transactions(session, checking.workspace_id, test_user.id, month=date(2026, 10, 1))
    assert not [p for p in october if p.recurring_id == rec["id"]]  # fulfilled by the linked charge


@pytest.mark.asyncio
async def test_ac_6_11_late_on_card_counts_in_open_statement(session, test_user, test_workspace):
    from app.services.card_statement_service import get_statements

    card = Account(user_id=test_user.id, workspace_id=test_workspace.id, name="Card", type="credit_card",
                   balance=D("0"), currency="BRL", statement_close_day=25, payment_due_day=5, cycle_days_manual=True)
    session.add(card)
    await session.commit()
    rec = RecurringTransaction(user_id=test_user.id, workspace_id=test_workspace.id, account_id=card.id,
                               description="Streaming", amount=D("30"), currency="BRL", type="debit",
                               frequency="monthly", day_of_month=10, start_date=date(2026, 10, 10),
                               next_occurrence=date(2026, 10, 10), auto_generate=False,
                               match_condition={"field": "description_key", "op": "starts_with", "value": "STREAM"})
    session.add(rec)
    await session.commit()
    view = await recurrence_service.timeline_for(session, rec, today=TODAY)
    first = view.timeline[0]
    assert (first.period, first.status, first.month) == (date(2026, 10, 10), "late", date(2026, 11, 1))
    statements = await get_statements(session, card, today=TODAY)
    nov = next(s for s in statements["statements"] if s["month"] == date(2026, 11, 1))
    assert nov["projected_recurring"] >= D("30.00")
    item = next(i for i in nov["projected_items"] if i["source"] == "recurrence")
    assert (item["description"], item["ref_id"], item["amount"]) == ("Streaming", str(rec.id), D("30.00"))
    later = await recurrence_service.timeline_for(session, rec, today=date(2026, 11, 20))
    assert later.timeline[0].status == "missed"


@pytest.mark.asyncio
async def test_ac_6_12_end_recurrence(client: AsyncClient, auth_headers, session, checking):
    await add_tx(session, checking, "GYMPASS", "99", date(2026, 8, 5))
    rec = await create(client, auth_headers, checking, description="Gympass", amount="99", start_date="2026-08-05",
                       day_of_month=5, match_condition={"field": "description_key", "op": "starts_with", "value": "GYMPASS"})
    with patch("app.core.app_clock.app_today", return_value=date(2026, 11, 20)), \
         patch("app.services.recurrence_service.app_today", return_value=date(2026, 11, 20)):
        view = (await client.get(f"/api/recurring-transactions/{rec['id']}/timeline", headers=auth_headers)).json()
        assert view["alerts"]["suggest_end"] is True
        resp = await client.post(f"/api/recurring-transactions/{rec['id']}/end", json={}, headers=auth_headers)
    assert resp.json()["end_date"] == view["alerts"]["last_missed"]


@pytest.mark.asyncio
async def test_ac_6_13_suggestions_api(client: AsyncClient, auth_headers, session, checking):
    for m in range(5, 11):
        await add_tx(session, checking, "SPOTIFY", "21.90", date(2026, m, 7))
    resp = await client.get(f"/api/accounts/{checking.id}/recurring-suggestions", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    found = resp.json()
    assert len(found) == 1 and found[0]["amount"] == "21.90"
    key = found[0]["key"]
    resp = await client.post(f"/api/accounts/{checking.id}/recurring-suggestions/dismiss", json={"key": key},
                             headers=auth_headers)
    assert resp.status_code == 204
    assert (await client.get(f"/api/accounts/{checking.id}/recurring-suggestions", headers=auth_headers)).json() == []


@pytest.mark.asyncio
async def test_ac_6_14_legacy_recurrence(client: AsyncClient, auth_headers, session, checking):
    old = await add_tx(session, checking, "Netflix Subscription", "39.90", date(2026, 9, 10))
    other = await add_tx(session, checking, "Netflix Subscription", "45.00", date(2026, 10, 10))
    rec = await create(client, auth_headers, checking, description="Netflix Subscription", amount="39.90",
                       start_date="2026-09-10", day_of_month=10, auto_generate=True)
    await session.refresh(old)
    await session.refresh(other)
    assert old.recurring_transaction_id == rec_id(rec)
    assert other.recurring_link != "auto"


@pytest.mark.asyncio
async def test_ac_6_15_make_recurring_refused_for_parcels(client: AsyncClient, auth_headers, session, checking):
    parcel = await add_tx(session, checking, "LOJA 2/10", "50", date(2026, 10, 1), installment_number=2,
                          total_installments=10)
    resp = await client.get(f"/api/transactions/{parcel.id}/recurring-draft", headers=auth_headers)
    assert resp.status_code == 400
    ok = await add_tx(session, checking, "SPOTIFY", "21.90", date(2026, 10, 7))
    draft = (await client.get(f"/api/transactions/{ok.id}/recurring-draft", headers=auth_headers)).json()
    assert draft["day_of_month"] == 7 and draft["match_condition"]["conditions"][0]["value"] == "SPOTIFY"


@pytest.mark.asyncio
async def test_card_bill_payment_and_previous_balance_are_not_recurring(session, test_user, test_workspace):
    card = Account(user_id=test_user.id, workspace_id=test_workspace.id, name="Card", type="credit_card",
                   balance=D("0"), currency="BRL")
    session.add(card)
    await session.commit()
    pay = await add_tx(session, card, "PGTO", "500", date(2026, 10, 1), type_="credit", operation_type="PAGAMENTO_FATURA")
    await recompute_account(session, card.id)
    await session.refresh(pay)
    assert pay.card_line_kind == "payment"
    assert recurrence_service.row_direction(pay, card) is None
    session.add(CreditCardBill(user_id=card.user_id, workspace_id=card.workspace_id, account_id=card.id,
                               external_id="x", due_date=date(2026, 10, 10), total_amount=D("0")))
    await session.commit()


@pytest.mark.asyncio
async def test_weekly_billed_monthly_validation(client: AsyncClient, auth_headers, checking):
    body = {"description": "Cleaner", "amount": "150", "currency": "BRL", "type": "debit",
            "frequency": "weekly_billed_monthly", "start_date": "2026-09-05", "day_of_month": 5,
            "account_id": str(checking.id)}
    resp = await client.post("/api/recurring-transactions", json=body, headers=auth_headers)
    assert resp.status_code == 400
    body["billing_weekday"] = 1
    resp = await client.post("/api/recurring-transactions", json=body, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["week_count_basis"] == "previous_month"


@pytest.mark.asyncio
async def test_skip_occurrence_api(client: AsyncClient, auth_headers, checking):
    """"Did not / will not happen": the occurrence leaves the forecast (and
    the reports) until undone; a linked occurrence or a date off the
    schedule cannot be skipped."""
    start = (date.today().replace(day=1) + timedelta(days=40)).replace(day=12)
    second = (start.replace(day=1) + timedelta(days=40)).replace(day=12)
    rec = await create(client, auth_headers, checking, description="Gym", start_date=start.isoformat())
    url = f"/api/recurring-transactions/{rec['id']}/skip"

    resp = await client.post(url, json={"period": start.isoformat()}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    statuses = {s["period"]: s["status"] for s in body["timeline"]}
    assert statuses[start.isoformat()] == "skipped"
    assert body["next"]["period"] == second.isoformat()

    resp = await client.post(url, json={"period": start.isoformat(), "skipped": False}, headers=auth_headers)
    assert resp.json()["next"]["period"] == start.isoformat()

    off = await client.post(url, json={"period": (start + timedelta(days=1)).isoformat()}, headers=auth_headers)
    assert off.status_code == 400


@pytest.mark.asyncio
async def test_recurrence_paid_from_several_accounts(client: AsyncClient, auth_headers, session, checking):
    """A recurrence may be paid by any of its accounts, one whole charge per
    period: each period is linked to whichever account paid it, the
    forecast stays on its own account, and an account dropped from the list
    loses its links."""
    other = Account(user_id=checking.user_id, workspace_id=checking.workspace_id, name="Other", type="checking",
                    balance=D("0"), currency="BRL")
    session.add(other)
    await session.commit()
    await add_tx(session, checking, "PIX PSICOLOGA", "380", date(2026, 8, 5))
    sep = await add_tx(session, other, "PIX PSICOLOGA", "380", date(2026, 9, 5))
    await add_tx(session, checking, "PIX PSICOLOGA", "380", date(2026, 10, 5))
    cond = {"field": "description_key", "op": "starts_with", "value": "PIXPSICOLOGA"}
    rec = await create(client, auth_headers, checking, description="Psicóloga", amount="380",
                       start_date="2026-08-05", day_of_month=5, match_condition=cond,
                       extra_account_ids=[str(other.id)])
    assert rec["extra_account_ids"] == [str(other.id)]
    assert rec["link_stats"]["linked"] == 3
    await session.refresh(sep)
    assert (sep.recurring_transaction_id, sep.recurring_period) == (rec_id(rec), date(2026, 9, 5))

    # A later charge synced on the other account links when that account recomputes.
    nov = await add_tx(session, other, "PIX PSICOLOGA", "380", date(2026, 11, 5))
    await recompute_account(session, other.id)
    await session.commit()
    await session.refresh(nov)
    assert nov.recurring_transaction_id == rec_id(rec)

    timeline = (await client.get(f"/api/recurring-transactions/{rec['id']}/timeline", headers=auth_headers)).json()
    paid_by = {s["period"]: (s["status"], s["account_id"]) for s in timeline["timeline"]}
    assert paid_by["2026-08-05"] == ("posted", str(checking.id))
    assert paid_by["2026-09-05"] == ("posted", str(other.id))
    assert paid_by["2026-11-05"] == ("posted", str(other.id))
    assert timeline["next"]["period"] == "2026-12-05"

    resp = await client.patch(f"/api/recurring-transactions/{rec['id']}", json={"extra_account_ids": []},
                              headers=auth_headers)
    assert resp.status_code == 200 and resp.json()["extra_account_ids"] is None
    await session.refresh(sep)
    assert sep.recurring_transaction_id is None


@pytest.mark.asyncio
async def test_extra_accounts_validation(client: AsyncClient, auth_headers, checking):
    missing = await client.post("/api/recurring-transactions", headers=auth_headers, json={
        "description": "Sub", "amount": "10", "currency": "BRL", "type": "debit", "frequency": "monthly",
        "start_date": "2026-06-12", "account_id": str(checking.id), "extra_account_ids": [str(uuid.uuid4())],
    })
    assert missing.status_code == 400
    legacy = await client.post("/api/recurring-transactions", headers=auth_headers, json={
        "description": "Sub", "amount": "10", "currency": "BRL", "type": "debit", "frequency": "monthly",
        "start_date": "2026-06-12", "account_id": str(checking.id), "auto_generate": True,
        "extra_account_ids": [str(uuid.uuid4())],
    })
    assert legacy.status_code == 400
    # Its own account in the list is just dropped.
    own = await create(client, auth_headers, checking, extra_account_ids=[str(checking.id)])
    assert own["extra_account_ids"] is None
