"""RFC toledo-features Phase 5 — installment plans (AC-5.x)."""
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.credit_card_bill import CreditCardBill
from app.models.installment_plan import InstallmentPlan
from app.models.transaction import Transaction
from app.services import rule_service
from app.services.card_statement_service import get_statements
from app.services.installment_plan_service import plan_view
from app.services.recompute.installments import (
    ParcelRow,
    derive_plan_fields,
    group_installments,
    project_plan,
    validate_group,
)
from app.services.recompute_service import recompute_account

D = Decimal
MARKERS = [r"\bPARC\s*=\s*\d{1,3}\s*", r"\b\d{1,2}\s+DE\s+\d{1,2}\b"]


def parcel(id, n, total, month, amount="10", desc="LOJA", pd=None, pt=None, **kw):
    return ParcelRow(id=id, description=desc, amount=D(amount), installment_number=n,
                     total_installments=total, month=month, date=month, purchase_date=pd,
                     payment_type=pt, strip_patterns=MARKERS, **kw)


# --- pure ----------------------------------------------------------------------


def test_ac_5_2_purchase_date_groups_despite_description_change():
    pd = date(2026, 7, 2)
    groups = group_installments([
        parcel("a", 1, 6, date(2026, 8, 1), desc="SHEIN", pd=pd),
        parcel("b", 2, 6, date(2026, 9, 1), desc="SHEIN COM 02 DE 06", pd=pd),
    ])
    assert len(groups) == 1 and len(groups[0].members) == 2


def test_fallback_key_without_purchase_date_uses_stripped_description_and_origin():
    groups = group_installments([
        parcel("a", 1, 3, date(2026, 8, 1), desc="PARC=103 PNEUSTORE"),
        parcel("b", 2, 3, date(2026, 9, 1), desc="PNEUSTORE"),
        parcel("c", 1, 3, date(2026, 9, 1), desc="PNEUSTORE"),  # another purchase, a month later
    ])
    assert sorted(len(g.members) for g in groups) == [1, 2]


def test_ac_5_3_same_day_purchases_split_by_amount():
    pd = date(2026, 7, 2)
    rows = []
    for amount, tag in (("33.10", "a"), ("49.90", "b"), ("80.00", "c")):
        rows += [parcel(f"{tag}{n}", n, 6, date(2026, 7 + n, 1), amount, "SHEIN", pd) for n in (1, 2)]
    groups = group_installments(rows)
    assert len(groups) == 3
    assert all(len(g.members) == 2 for g in groups)
    assert len({g.key for g in groups}) == 3


def test_ac_5_4_first_parcel_cents_tolerance():
    pd = date(2026, 1, 5)
    rows = [parcel("p1", 1, 12, date(2026, 2, 1), "7.03", pd=pd)] + [
        parcel(f"p{n}", n, 12, date(2026, n + 1, 1), "6.92", pd=pd) for n in range(2, 12)
    ]
    (group,) = group_installments(rows)
    issues = validate_group(group.members, group.first_month, 12)
    assert all(v is None for v in issues.values())


def test_ac_5_5_out_of_sequence_parcel_is_a_conflict():
    pd = date(2026, 7, 20)
    rows = [
        parcel("p1", 1, 10, date(2026, 8, 1), pd=pd),
        parcel("p2", 2, 10, date(2026, 9, 1), pd=pd),
        parcel("p3", 3, 10, date(2026, 11, 1), pd=pd),
    ]
    (group,) = group_installments(rows)
    issues = validate_group(group.members, group.first_month, 10)
    assert group.first_month == date(2026, 8, 1)
    assert issues["p3"] == {"code": "month", "expected": "2026-10-01"}
    assert issues["p1"] is None


def test_amount_conflict():
    pd = date(2026, 7, 20)
    rows = [parcel(f"p{n}", n, 4, date(2026, 7 + n, 1), "10", pd=pd) for n in (1, 2, 3)]
    rows.append(parcel("p4", 4, 4, date(2026, 11, 1), "19", pd=pd))
    (group,) = group_installments(rows)
    assert validate_group(group.members, group.first_month, 4)["p4"]["code"] == "amount"


def test_ac_5_9_single_payment_is_not_a_plan():
    assert group_installments([parcel("x", 1, 12, date(2026, 8, 1), pt="SINGLE")]) == []


def test_ac_5_8_projection_missing_vs_projected():
    out = project_plan(
        total_installments=10, first_month=date(2026, 8, 1), installment_amount=D("50"),
        known_numbers={1, 2, 4}, open_month=date(2026, 11, 1), history_start=date(2026, 8, 1),
    )
    by_n = {p.number: p for p in out}
    assert by_n[3].kind == "missing" and by_n[3].month == date(2026, 10, 1)
    assert [n for n, p in by_n.items() if p.kind == "projected"] == [5, 6, 7, 8, 9, 10]
    assert by_n[5].month == date(2026, 12, 1) and by_n[10].month == date(2027, 5, 1)
    before = project_plan(
        total_installments=3, first_month=date(2026, 5, 1), installment_amount=D("1"),
        known_numbers={3}, open_month=date(2026, 11, 1), history_start=date(2026, 7, 1),
    )
    assert {p.number: p.kind for p in before} == {1: "before_history", 2: "before_history"}


def test_plan_fields_name_from_latest_parcel():
    fields = derive_plan_fields([
        parcel("a", 1, 3, date(2026, 8, 1), desc="PARC=103 PNEUSTORE", pd=date(2026, 8, 14)),
        parcel("b", 2, 3, date(2026, 9, 1), desc="PARC=103 PNEUSTORE", pd=date(2026, 8, 14)),
    ])
    assert fields["description"] == "PNEUSTORE"
    assert fields["first_month"] == date(2026, 8, 1)


# --- service -------------------------------------------------------------------


@pytest.fixture
async def card(session: AsyncSession, test_user, test_workspace) -> Account:
    await rule_service.install_builtin_pack(session, test_workspace.id, test_user.id, "BR_CARDS")
    acc = Account(user_id=test_user.id, workspace_id=test_workspace.id, name="Card", type="credit_card",
                  balance=D("0"), currency="BRL", statement_close_day=3, payment_due_day=10,
                  cycle_days_manual=True)
    session.add(acc)
    await session.commit()
    return acc


async def add_bill(session, account, ext, due, closing):
    session.add(CreditCardBill(user_id=account.user_id, workspace_id=account.workspace_id, account_id=account.id,
                               external_id=ext, due_date=due, total_amount=D("0"), closing_date=closing))
    await session.commit()


async def add_parcel(session, account, desc, n, total, d, amount="120", bill=None, pd=None, **kw) -> Transaction:
    tx = Transaction(user_id=account.user_id, workspace_id=account.workspace_id, account_id=account.id,
                     description=desc, original_description=desc, amount=D(amount), currency="BRL", date=d,
                     type="debit", source="sync", installment_number=n, total_installments=total,
                     installment_purchase_date=pd, provider_bill_external_id=bill,
                     external_id=kw.pop("external_id", f"{desc}-{n}-{d}"), **kw)
    session.add(tx)
    await session.commit()
    return tx


@pytest.mark.asyncio
async def test_ac_5_1_grouped_by_purchase_date_and_projected(session, card):
    await add_bill(session, card, "AUG", date(2026, 8, 10), date(2026, 8, 3))
    await add_bill(session, card, "SEP", date(2026, 9, 10), date(2026, 9, 3))
    pd = date(2026, 7, 14)
    await add_parcel(session, card, "PARC=103 PNEUSTORE", 1, 3, date(2026, 7, 14), bill="AUG", pd=pd)
    await add_parcel(session, card, "PARC=103 PNEUSTORE", 2, 3, date(2026, 7, 14), bill="SEP", pd=pd)
    await recompute_account(session, card.id)
    await session.commit()
    plans = (await session.execute(select(InstallmentPlan))).scalars().all()
    assert len(plans) == 1
    plan = plans[0]
    assert (plan.description, plan.total_installments, plan.first_month) == ("PNEUSTORE", 3, date(2026, 8, 1))
    view = await plan_view(session, card, today=date(2026, 9, 20))
    kinds = {i["number"]: (i["kind"], i["month"]) for i in view["plans"][0]["installments"]}
    assert kinds[3] == ("projected", date(2026, 10, 1))
    statements = await get_statements(session, card, today=date(2026, 9, 20))
    october = next(s for s in statements["statements"] if s["month"] == date(2026, 10, 1))
    assert october["projected_installments"] == D("120.00")
    assert october["projected_installment_count"] == 1
    # The account page lists the projected parcel in that month.
    assert october["projected_items"] == [{
        "source": "installment", "amount": D("120.00"), "description": "PNEUSTORE",
        "number": 3, "total": 3, "ref_id": str(plan.id), "category_id": None,
    }]
    assert all(s["projected_items"] == [] for s in statements["statements"] if s["state"] == "closed")


@pytest.mark.asyncio
async def test_ac_4_statement_month_filter_lists_rows_by_reference_month(
    session, card, client: AsyncClient, auth_headers,
):
    """Rows of a statement are those whose effective date falls in its due
    month, whatever their transaction date (RFC §6.4)."""
    await add_bill(session, card, "AUG", date(2026, 8, 10), date(2026, 8, 3))
    await add_bill(session, card, "SEP", date(2026, 9, 10), date(2026, 9, 3))
    pd = date(2026, 7, 14)
    p1 = await add_parcel(session, card, "PARC=103 PNEUSTORE", 1, 3, date(2026, 7, 14), bill="AUG", pd=pd)
    p2 = await add_parcel(session, card, "PARC=103 PNEUSTORE", 2, 3, date(2026, 7, 14), bill="SEP", pd=pd)
    await recompute_account(session, card.id)
    await session.commit()
    for month, expected in (("2026-08-01", {str(p1.id)}), ("2026-09-01", {str(p2.id)}), ("2026-07-01", set())):
        resp = await client.get(
            "/api/transactions", params={"account_id": str(card.id), "statement_month": month},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert {t["id"] for t in resp.json()["items"]} == expected


@pytest.mark.asyncio
async def test_plan_ids_are_stable_and_recompute_idempotent(session, card):
    pd = date(2026, 7, 14)
    await add_parcel(session, card, "LOJA", 1, 3, date(2026, 7, 14), pd=pd)
    await recompute_account(session, card.id)
    first = (await session.execute(select(InstallmentPlan))).scalar_one().id
    await add_parcel(session, card, "LOJA", 2, 3, date(2026, 8, 14), pd=pd)
    await recompute_account(session, card.id)
    assert (await session.execute(select(InstallmentPlan))).scalar_one().id == first
    assert (await recompute_account(session, card.id)).changed == 0


@pytest.mark.asyncio
async def test_ac_5_6_and_5_7_manual_link_and_merge_guard(session, card, client: AsyncClient, auth_headers):
    a1 = await add_parcel(session, card, "LOJA A", 1, 6, date(2026, 8, 5), pd=date(2026, 8, 5))
    a2 = await add_parcel(session, card, "LOJA A", 2, 6, date(2026, 9, 5), pd=date(2026, 8, 5))
    b2 = await add_parcel(session, card, "LOJA B", 2, 6, date(2026, 9, 6), "60", pd=date(2026, 8, 6))
    await recompute_account(session, card.id)
    await session.commit()
    for t in (a1, a2, b2):
        await session.refresh(t)
    plan_a, plan_b = a1.installment_plan_id, b2.installment_plan_id
    assert plan_a != plan_b

    resp = await client.post(f"/api/installment-plans/{plan_b}/merge", json={"into_plan_id": str(plan_a)},
                             headers=auth_headers)
    assert resp.status_code == 400
    assert "Both plans have installment 2" in resp.json()["detail"]

    # split a1 into a plan of its own, then link it back manually
    resp = await client.post(f"/api/transactions/{a1.id}/installment-plan", json={"plan_id": "new"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    own_plan = resp.json()["installment_plan_id"]
    assert own_plan not in (str(plan_a), None) and resp.json()["installment_link"] == "manual"
    for _ in range(2):
        await recompute_account(session, card.id)
    await session.refresh(a1)
    assert (str(a1.installment_plan_id), a1.installment_link) == (own_plan, "manual")

    # undo goes back to the heuristic and the manual plan disappears
    resp = await client.post(f"/api/transactions/{a1.id}/installment-plan", json={"plan_id": None}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["installment_link"] == "auto"
    import uuid

    assert await session.get(InstallmentPlan, uuid.UUID(own_plan)) is None


@pytest.mark.asyncio
async def test_merge_moves_parcels(session, card, client: AsyncClient, auth_headers):
    a1 = await add_parcel(session, card, "LOJA", 1, 4, date(2026, 8, 5), pd=date(2026, 8, 5))
    b2 = await add_parcel(session, card, "LOJA X", 2, 4, date(2026, 9, 5), pd=date(2026, 8, 7))
    await recompute_account(session, card.id)
    await session.commit()
    await session.refresh(a1)
    await session.refresh(b2)
    resp = await client.post(f"/api/installment-plans/{b2.installment_plan_id}/merge",
                             json={"into_plan_id": str(a1.installment_plan_id)}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    await session.refresh(b2)
    assert (b2.installment_plan_id, b2.installment_link) == (a1.installment_plan_id, "manual")


@pytest.mark.asyncio
async def test_confirm_conflict(session, card, client: AsyncClient, auth_headers):
    pd = date(2026, 7, 20)
    await add_parcel(session, card, "LOJA", 1, 10, date(2026, 7, 20), pd=pd)  # due Aug
    await add_parcel(session, card, "LOJA", 2, 10, date(2026, 8, 20), pd=pd)  # due Sep
    late = await add_parcel(session, card, "LOJA", 3, 10, date(2026, 10, 20), pd=pd)  # due Nov
    await recompute_account(session, card.id)
    await session.commit()
    await session.refresh(late)
    assert late.installment_link == "conflict"
    assert late.installment_issue == {"code": "month", "expected": "2026-10-01"}
    resp = await client.post(f"/api/transactions/{late.id}/installment-plan/confirm", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["installment_link"] == "manual"


@pytest.mark.asyncio
async def test_ac_5_10_category_follows_the_purchase(session, card, test_categories, client: AsyncClient, auth_headers):
    pd = date(2026, 8, 1)
    await add_parcel(session, card, "AUTO PECAS", 1, 3, date(2026, 8, 1), pd=pd)
    p2 = await add_parcel(session, card, "AUTO PECAS", 2, 3, date(2026, 9, 1), pd=pd)
    await recompute_account(session, card.id)
    await session.commit()
    resp = await client.patch(f"/api/transactions/{p2.id}", json={"category_id": str(test_categories[1].id)},
                              headers=auth_headers)
    assert resp.status_code == 200
    p3 = await add_parcel(session, card, "AUTO PECAS", 3, 3, date(2026, 10, 1), pd=pd)
    await recompute_account(session, card.id)
    await session.refresh(p3)
    assert p3.category_id == test_categories[1].id


@pytest.mark.asyncio
async def test_ac_5_11_manual_series_are_plans(session, card, client: AsyncClient, auth_headers):
    resp = await client.post("/api/transactions/installments", json={
        "base": {"account_id": str(card.id), "description": "Notebook", "amount": "250.00",
                 "date": "2026-08-05", "type": "debit"},
        "installments": 4,
    }, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    view = (await client.get(f"/api/accounts/{card.id}/installment-plans", headers=auth_headers)).json()
    series = [p for p in view["plans"] if p["source"] == "series"]
    assert len(series) == 1 and series[0]["total_installments"] == 4
    assert all(i["kind"] == "posted" for i in series[0]["installments"])
    await recompute_account(session, card.id)
    assert (await session.execute(
        select(InstallmentPlan).where(InstallmentPlan.source == "detected")
    )).first() is None


@pytest.mark.asyncio
async def test_removed_rows_are_not_parcels(session, card):
    from datetime import datetime, timezone

    pd = date(2026, 8, 1)
    await add_parcel(session, card, "LOJA", 1, 3, date(2026, 8, 1), pd=pd)
    await add_parcel(session, card, "LOJA", 1, 3, date(2026, 8, 1), pd=pd, external_id="dup",
                     is_ignored=True, ignored_reason="provider_removed",
                     provider_removed_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    await recompute_account(session, card.id)
    dup = (await session.execute(select(Transaction).where(Transaction.external_id == "dup"))).scalar_one()
    assert dup.installment_plan_id is None
