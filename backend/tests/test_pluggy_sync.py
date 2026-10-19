"""RFC toledo-features Phase 2 — Pluggy data capture and sync semantics (AC-2.x)."""
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.credit_card_bill import CreditCardBill
from app.models.transaction import Transaction
from app.providers.base import TransactionData
from app.schemas.transaction import TransactionUpdate
from app.services import connection_service, transaction_service
from app.services.pluggy_sync.removals import RemovalCandidate, plan_removals
from app.services.pluggy_sync.sweeps import plan_sweeps, union_by_id
from app.services.recompute.duplicates import DedupRow, find_pending_duplicates
from app.services.recompute_service import recompute_account
from tests.pluggy_fakes import FakePluggy, pluggy_account, pluggy_bill, pluggy_tx

TODAY = date(2026, 10, 17)
CARD = "acc-card"
BANK = "acc-bank"


# --- pure: sweeps -----------------------------------------------------------


def _data(ext, updated=None, amount="10"):
    return TransactionData(
        external_id=ext, description="x", amount=Decimal(amount), date=TODAY, type="debit",
        provider_updated_at=updated,
    )


def test_plan_sweeps_full_without_watermark():
    plan = plan_sweeps(None, TODAY, 60)
    assert plan.full and plan.window_from is None and plan.created_at_from is None


def test_plan_sweeps_incremental():
    wm = datetime(2026, 10, 10, tzinfo=timezone.utc)
    plan = plan_sweeps(wm, TODAY, 60)
    assert not plan.full
    assert plan.window_from == TODAY - timedelta(days=60)
    assert plan.created_at_from == wm - timedelta(hours=24)


def test_union_by_id_dedups_and_keeps_freshest():
    old = datetime(2026, 10, 1, tzinfo=timezone.utc)
    new = datetime(2026, 10, 2, tzinfo=timezone.utc)
    merged = union_by_id(
        [_data("a", new, "1"), _data("b")],
        [_data("a", old, "2"), _data("c")],
    )
    by_id = {t.external_id: t for t in merged}
    assert set(by_id) == {"a", "b", "c"}
    assert by_id["a"].amount == Decimal("1")  # the stale copy did not win


# --- pure: removal guard ----------------------------------------------------


def _cands(n, start=date(2026, 10, 1)):
    return [RemovalCandidate(id=i, external_id=f"t{i}", date=start) for i in range(n)]


def test_removal_marks_missing_rows_in_window():
    plan = plan_removals(
        _cands(40), returned_ids={f"t{i}" for i in range(39)}, full=False,
        window_from=date(2026, 9, 1), refetched_ids=set(), window_count=39,
    )
    assert plan.remove == [39] and not plan.guarded


def test_removal_guard_on_mass_absence():
    plan = plan_removals(
        _cands(40), returned_ids={f"t{i}" for i in range(20)}, full=False,
        window_from=date(2026, 9, 1), refetched_ids=set(), window_count=20,
    )
    assert plan.remove == [] and plan.guarded


def test_removal_guard_on_empty_answer():
    plan = plan_removals(
        _cands(3), returned_ids=set(), full=True, window_from=None,
        refetched_ids=set(), window_count=0,
    )
    assert plan.remove == [] and plan.guarded


def test_removal_ignores_rows_outside_window_unless_refetched():
    rows = [
        RemovalCandidate(id=1, external_id="old", date=date(2026, 1, 1)),
        RemovalCandidate(id=2, external_id="old-pending", date=date(2026, 1, 1)),
        RemovalCandidate(id=3, external_id="in", date=date(2026, 10, 1)),
    ]
    plan = plan_removals(
        rows, returned_ids={"in"}, full=False, window_from=date(2026, 9, 1),
        refetched_ids={"old-pending"}, window_count=1,
    )
    assert plan.remove == [2]


# --- pure: duplicates -------------------------------------------------------


def _dup(id, d, amount, status, bill, n=None, total=None, purchase=None, at=None, **kw):
    return DedupRow(
        id=id, date=d, signed_amount=Decimal(amount), currency="BRL", status=status,
        has_bill_link=bill, installment_number=n, total_installments=total,
        purchase_date=purchase, transaction_at=at, **kw,
    )


def test_duplicates_one_to_one_nearest_first():
    rows = [
        _dup("p1", date(2026, 9, 28), "-120", "pending", False, 3, 10),
        _dup("p2", date(2026, 9, 30), "-120", "pending", False, 3, 10),
        _dup("q1", date(2026, 10, 1), "-120", "posted", True, 3, 10),
    ]
    assert find_pending_duplicates(rows) == {"p2": "q1"}


def test_duplicates_require_same_installment_number_and_window():
    rows = [
        _dup("p1", date(2026, 9, 1), "-50", "pending", False, 2, 6),
        _dup("q1", date(2026, 10, 1), "-50", "posted", True, 3, 6),
        _dup("p2", date(2026, 9, 1), "-70", "pending", False),
        _dup("q2", date(2026, 9, 10), "-70", "posted", True),
    ]
    assert find_pending_duplicates(rows) == {}


def test_duplicates_cents_tolerance_by_purchase_date():
    pd = date(2026, 8, 14)
    rows = [
        _dup("p", date(2026, 9, 20), "-50.01", "pending", False, 2, 6, pd),
        _dup("q", date(2026, 10, 1), "-49.99", "posted", True, 2, 6, pd),
    ]
    assert find_pending_duplicates(rows) == {"p": "q"}


def test_duplicates_extra_copies_by_timestamp():
    at = datetime(2026, 9, 28, 14, 3, 11, tzinfo=timezone.utc)
    rows = [
        _dup("p1", date(2026, 9, 28), "-120", "pending", False, 3, 10, at=at),
        _dup("p2", date(2026, 9, 28), "-120", "pending", False, 3, 10, at=at.replace(microsecond=5)),
        _dup("q1", date(2026, 10, 1), "-120", "posted", True, 3, 10, at=at),
    ]
    assert find_pending_duplicates(rows) == {"p1": "q1", "p2": "q1"}


def test_duplicates_echo_cannot_absorb_and_override_is_respected():
    rows = [
        _dup("p", date(2026, 10, 1), "500", "pending", False),
        _dup("echo", date(2026, 10, 1), "500", "posted", True),
        _dup("p2", date(2026, 10, 1), "-30", "pending", False, override=True),
        _dup("q2", date(2026, 10, 1), "-30", "posted", True),
    ]
    assert find_pending_duplicates(rows, {"echo"}) == {}


# --- service helpers --------------------------------------------------------


@pytest.fixture
def frozen_today():
    with patch("app.services.pluggy_sync.account.app_today", return_value=TODAY):
        yield TODAY


@pytest.fixture
async def pluggy_conn(session: AsyncSession, test_user, test_workspace) -> BankConnection:
    conn = BankConnection(
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        provider="pluggy",
        external_id="item-1",
        institution_name="Banco",
        credentials={"item_id": "item-1"},
        status="active",
    )
    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    return conn


@pytest.fixture
def fake() -> FakePluggy:
    f = FakePluggy()
    f.accounts = [pluggy_account(CARD, kind="CREDIT"), pluggy_account(BANK, kind="BANK", name="Checking")]
    return f


async def run_sync(session, conn, fake):
    with patch("app.services.connection_service.get_provider", return_value=fake):
        await connection_service.sync_connection(session, conn.id, conn.workspace_id, conn.user_id)


async def tx_by_ext(session, ext) -> Transaction:
    row = (
        await session.execute(select(Transaction).where(Transaction.external_id == ext))
    ).scalar_one()
    await session.refresh(row)
    return row


async def account_of(session, external_id) -> Account:
    acc = (await session.execute(select(Account).where(Account.external_id == external_id))).scalar_one()
    await session.refresh(acc)
    return acc


@contextmanager
def count_updates(session, table="transactions"):
    """Rows written by UPDATE statements on ``table`` while the block runs."""
    written: list[tuple[str, int]] = []
    engine = session.bind.sync_engine if hasattr(session.bind, "sync_engine") else session.bind

    def listener(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith(f"UPDATE {table.upper()} ") and cursor.rowcount:
            written.append((statement, cursor.rowcount))

    event.listen(engine, "after_cursor_execute", listener)
    try:
        yield written
    finally:
        event.remove(engine, "after_cursor_execute", listener)


# --- AC-2.1 / INV-3 ---------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_2_1_provider_changes_are_applied(session, pluggy_conn, fake, frozen_today):
    fake.put(pluggy_tx("t1", description="UBER *TRIP", amount=-25.00, when=date(2026, 10, 1), status="PENDING"))
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "t1")
    assert (row.amount, row.status) == (Decimal("25.00"), "pending")

    changed = pluggy_tx(
        "t1", description="UBER *TRIP", amount=-27.40, when=date(2026, 10, 2), status="POSTED",
        created_at=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
    )
    fake.put(changed)
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "t1")
    assert row.amount == Decimal("27.40")
    assert row.date == date(2026, 10, 2)
    assert row.status == "posted"
    assert row.raw_data == changed


@pytest.mark.asyncio
async def test_inv_3_second_sync_writes_nothing(session, pluggy_conn, fake, frozen_today):
    fake.put(
        pluggy_tx("a", amount=-10, when=date(2026, 10, 1)),
        pluggy_tx("b", amount=-20, when=date(2026, 10, 2), status="PENDING"),
        pluggy_tx("c", account_id=BANK, amount=100, when=date(2026, 10, 3)),
    )
    await run_sync(session, pluggy_conn, fake)
    await run_sync(session, pluggy_conn, fake)  # watermark set, incremental
    with count_updates(session) as updates:
        await run_sync(session, pluggy_conn, fake)
    assert updates == []
    # The probe does see writes when something changed.
    fake.put(pluggy_tx("a", amount=-11, when=date(2026, 10, 1)))
    with count_updates(session) as updates:
        await run_sync(session, pluggy_conn, fake)
    assert updates


# --- AC-2.2 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_2_2_user_owned_fields_survive(session, pluggy_conn, fake, frozen_today, test_categories, test_user):
    fake.put(pluggy_tx("t1", description="MERCADO X", amount=-50, when=date(2026, 10, 5)))
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "t1")
    await transaction_service.update_transaction(
        session, row.id, pluggy_conn.workspace_id, test_user.id,
        TransactionUpdate(description="Groceries for the party", notes="birthday", category_id=test_categories[0].id),
    )

    fake.put(pluggy_tx("t1", description="MERCADO X LTDA", amount=-55, when=date(2026, 10, 5)))
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "t1")
    assert row.amount == Decimal("55")
    assert row.description == "Groceries for the party"
    assert row.notes == "birthday"
    assert row.category_id == test_categories[0].id
    assert row.original_description == "MERCADO X LTDA"


@pytest.mark.asyncio
async def test_description_follows_bank_when_not_renamed(session, pluggy_conn, fake, frozen_today):
    fake.put(pluggy_tx("t1", description="PADARIA", amount=-5, when=date(2026, 10, 5)))
    await run_sync(session, pluggy_conn, fake)
    fake.put(pluggy_tx("t1", description="PADARIA DO ZE", amount=-5, when=date(2026, 10, 5)))
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "t1")
    assert row.description == "PADARIA DO ZE"
    assert row.original_description == "PADARIA DO ZE"


# --- AC-2.3 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_2_3_sync_lock(session, pluggy_conn, fake, frozen_today, test_user):
    fake.put(pluggy_tx("t1", description="LOJA", amount=-80, when=date(2026, 10, 5), status="PENDING"))
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "t1")
    await transaction_service.set_sync_lock(session, row.id, pluggy_conn.workspace_id, test_user.id, True)

    newer = pluggy_tx(
        "t1", description="LOJA ABC", amount=-95, when=date(2026, 10, 6), status="POSTED",
        operation_type="COMPRA",
    )
    fake.put(newer)
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "t1")
    assert (row.amount, row.date, row.description, row.status) == (
        Decimal("80"), date(2026, 10, 5), "LOJA", "pending",
    )
    assert row.raw_data == newer
    assert row.operation_type == "COMPRA"
    assert row.provider_description_raw == "LOJA ABC"

    await transaction_service.set_sync_lock(session, row.id, pluggy_conn.workspace_id, test_user.id, False)
    row = await tx_by_ext(session, "t1")
    assert (row.amount, row.date, row.description, row.status) == (
        Decimal("95"), date(2026, 10, 6), "LOJA ABC", "posted",
    )


# --- AC-2.4 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_2_4_rekey_by_provider_id(session, pluggy_conn, fake, frozen_today):
    fake.put(pluggy_tx("old", description="NETFLIX", amount=-39.9, when=date(2026, 10, 3), provider_id="OF-123"))
    await run_sync(session, pluggy_conn, fake)
    original = await tx_by_ext(session, "old")

    fake.drop(CARD, "old")
    fake.put(pluggy_tx(
        "new", description="NETFLIX.COM", amount=-39.9, when=date(2026, 10, 3), provider_id="OF-123",
        created_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
    ))
    await run_sync(session, pluggy_conn, fake)

    rows = (await session.execute(select(Transaction).where(Transaction.provider_id == "OF-123"))).scalars().all()
    assert len(rows) == 1
    await session.refresh(rows[0])
    assert rows[0].id == original.id
    assert rows[0].external_id == "new"
    assert rows[0].provider_removed_at is None
    assert rows[0].is_ignored is False


# --- AC-2.5 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_2_5_back_dated_rows_are_fetched(session, pluggy_conn, fake, frozen_today):
    fake.put(pluggy_tx("seed", amount=-10, when=date(2026, 10, 9), created_at=datetime(2026, 10, 10, tzinfo=timezone.utc)))
    await run_sync(session, pluggy_conn, fake)
    card = await account_of(session, CARD)
    assert card.provider_tx_created_watermark.replace(tzinfo=timezone.utc) == datetime(2026, 10, 10, tzinfo=timezone.utc)

    ingested = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)
    fake.put(pluggy_tx(
        "parcel", description="LOJA 4/6", amount=-30, when=date(2026, 6, 3),
        created_at=ingested, installment=(4, 6),
    ))
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "parcel")
    assert row.date == date(2026, 6, 3)
    card = await account_of(session, CARD)
    assert card.provider_tx_created_watermark.replace(tzinfo=timezone.utc) == ingested
    sweep_args = [c for c in fake.transaction_calls() if c[3]]
    assert sweep_args, "an ingestion-window sweep was made"


# --- AC-2.6 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_2_6_old_pending_rows_are_refetched_by_id(session, pluggy_conn, fake, frozen_today):
    fake.bills[CARD] = [pluggy_bill("B10", date(2026, 10, 10), 99.0)]
    fake.put(pluggy_tx(
        "p-old", amount=-99, when=date(2026, 7, 1), status="PENDING",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    ))
    await run_sync(session, pluggy_conn, fake)

    # Now posted with the bill — but Pluggy's own createdAt is old, and the
    # date is outside the 60-day window: only the id refetch can see it.
    fake.put(pluggy_tx(
        "p-old", amount=-99, when=date(2026, 7, 1), status="POSTED", bill_id="B10",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    ))
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "p-old")
    assert row.status == "posted"
    assert row.provider_bill_external_id == "B10"
    bill = (await session.execute(select(CreditCardBill).where(CreditCardBill.external_id == "B10"))).scalar_one()
    assert row.bill_id == bill.id
    assert any(c[4] == ("p-old",) for c in fake.transaction_calls())


# --- AC-2.7 / AC-2.8 --------------------------------------------------------


async def _seed_forty(session, pluggy_conn, fake):
    fake.put(*[
        pluggy_tx(f"r{i}", amount=-(i + 1), when=date(2026, 10, 1) + timedelta(days=i % 15))
        for i in range(40)
    ])
    await run_sync(session, pluggy_conn, fake)


@pytest.mark.asyncio
async def test_ac_2_7_removal_by_absence_is_soft_and_reversible(session, pluggy_conn, fake, frozen_today):
    await _seed_forty(session, pluggy_conn, fake)
    gone = next(r for r in fake.transactions[CARD] if r["id"] == "r7")
    fake.drop(CARD, "r7")
    await run_sync(session, pluggy_conn, fake)

    row = await tx_by_ext(session, "r7")
    assert row.provider_removed_at is not None
    assert row.is_ignored is True
    assert row.ignored_reason == "provider_removed"

    removed_list, total, _ = await transaction_service.get_transactions(
        session, pluggy_conn.workspace_id, pluggy_conn.user_id, sync_state="removed",
    )
    assert [t.external_id for t in removed_list] == ["r7"]

    fake.put(gone)
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "r7")
    assert row.provider_removed_at is None
    assert row.is_ignored is False
    assert row.ignored_reason is None


@pytest.mark.asyncio
async def test_ac_2_8_removal_safety_guard(session, pluggy_conn, fake, frozen_today, caplog):
    await _seed_forty(session, pluggy_conn, fake)
    fake.drop(CARD, *[f"r{i}" for i in range(20)])
    caplog.set_level(logging.WARNING)
    await run_sync(session, pluggy_conn, fake)
    removed = (
        await session.execute(select(Transaction).where(Transaction.provider_removed_at.is_not(None)))
    ).scalars().all()
    assert removed == []
    assert "safety limit" in caplog.text


@pytest.mark.asyncio
async def test_user_ignored_row_is_not_revived_and_keeps_reason(session, pluggy_conn, fake, frozen_today):
    fake.put(pluggy_tx("t1", amount=-10, when=date(2026, 10, 5)))
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "t1")
    await transaction_service.toggle_ignore_transaction(session, row.id, pluggy_conn.workspace_id)
    row = await tx_by_ext(session, "t1")
    assert (row.is_ignored, row.ignored_reason) == (True, "user")

    fake.put(pluggy_tx("t1", amount=-12, when=date(2026, 10, 5)))
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "t1")
    assert row.amount == Decimal("10")  # frozen
    assert (row.is_ignored, row.ignored_reason) == (True, "user")


# --- AC-2.9 / 2.10 / 2.11 ---------------------------------------------------


@pytest.mark.asyncio
async def test_ac_2_9_pending_copy_flagged_not_deleted(session, pluggy_conn, fake, frozen_today):
    fake.bills[CARD] = [pluggy_bill("B11", date(2026, 11, 10), 120.0)]
    fake.put(
        pluggy_tx("pend", description="PNEUSTORE 3/10", amount=-120, when=date(2026, 9, 28),
                  status="PENDING", installment=(3, 10)),
        pluggy_tx("post", description="PNEUSTORE 3/10", amount=-120, when=date(2026, 10, 1),
                  status="POSTED", installment=(3, 10), bill_id="B11"),
    )
    await run_sync(session, pluggy_conn, fake)
    pending = await tx_by_ext(session, "pend")
    posted = await tx_by_ext(session, "post")
    assert pending.duplicate_of_id == posted.id
    assert (pending.is_ignored, pending.ignored_reason) == (True, "duplicate")
    assert posted.is_ignored is False and posted.duplicate_of_id is None


@pytest.mark.asyncio
async def test_ac_2_10_installment_cents_drift_still_matches(session, pluggy_conn, fake, frozen_today):
    fake.bills[CARD] = [pluggy_bill("B11", date(2026, 11, 10), 49.99)]
    fake.put(
        pluggy_tx("pend", amount=-50.01, when=date(2026, 9, 20), status="PENDING",
                  installment=(2, 6), purchase_date="2026-08-14"),
        pluggy_tx("post", amount=-49.99, when=date(2026, 10, 1), status="POSTED",
                  installment=(2, 6), purchase_date="2026-08-14", bill_id="B11"),
    )
    await run_sync(session, pluggy_conn, fake)
    pending = await tx_by_ext(session, "pend")
    posted = await tx_by_ext(session, "post")
    assert pending.duplicate_of_id == posted.id


@pytest.mark.asyncio
async def test_ac_2_11_not_a_duplicate(client, auth_headers, session, pluggy_conn, fake, frozen_today):
    fake.bills[CARD] = [pluggy_bill("B11", date(2026, 11, 10), 120.0)]
    fake.put(
        pluggy_tx("pend", amount=-120, when=date(2026, 9, 28), status="PENDING"),
        pluggy_tx("post", amount=-120, when=date(2026, 10, 1), status="POSTED", bill_id="B11"),
    )
    await run_sync(session, pluggy_conn, fake)
    pending = await tx_by_ext(session, "pend")
    listed = await client.get("/api/transactions", params={"sync_state": "duplicate"}, headers=auth_headers)
    assert [t["external_id"] for t in listed.json()["items"]] == ["pend"]

    resp = await client.post(f"/api/transactions/{pending.id}/not-duplicate", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_ignored"] is False and body["duplicate_of_id"] is None
    assert body["duplicate_override"] is True

    await run_sync(session, pluggy_conn, fake)
    await recompute_account(session, pending.account_id)
    pending = await tx_by_ext(session, "pend")
    assert pending.is_ignored is False and pending.duplicate_of_id is None


@pytest.mark.asyncio
async def test_unignoring_a_duplicate_keeps_it(session, pluggy_conn, fake, frozen_today):
    fake.bills[CARD] = [pluggy_bill("B11", date(2026, 11, 10), 120.0)]
    fake.put(
        pluggy_tx("pend", amount=-120, when=date(2026, 9, 28), status="PENDING"),
        pluggy_tx("post", amount=-120, when=date(2026, 10, 1), status="POSTED", bill_id="B11"),
    )
    await run_sync(session, pluggy_conn, fake)
    pending = await tx_by_ext(session, "pend")
    await transaction_service.toggle_ignore_transaction(session, pending.id, pluggy_conn.workspace_id)
    await run_sync(session, pluggy_conn, fake)
    pending = await tx_by_ext(session, "pend")
    assert pending.is_ignored is False and pending.duplicate_override is True


# --- AC-2.12 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_2_12_missing_bill_fetched_by_id(session, pluggy_conn, fake, frozen_today):
    fake.hidden_bills["B9"] = pluggy_bill("B9", date(2026, 10, 10), 77.0, closing=date(2026, 10, 3))
    fake.put(pluggy_tx("t1", amount=-77, when=date(2026, 9, 20), bill_id="B9"))
    await run_sync(session, pluggy_conn, fake)

    assert ("bill", "B9") in fake.calls
    bill = (await session.execute(select(CreditCardBill).where(CreditCardBill.external_id == "B9"))).scalar_one()
    assert bill.closing_date == date(2026, 10, 3)
    assert bill.allows_installments is True
    row = await tx_by_ext(session, "t1")
    assert row.bill_id == bill.id
    assert row.effective_date == date(2026, 10, 10)


# --- capture of §4.3 fields --------------------------------------------------


@pytest.mark.asyncio
async def test_provider_fields_are_captured(session, pluggy_conn, fake, frozen_today):
    fake.accounts = [pluggy_account(CARD, credit_data={
        "creditLimit": 5000, "availableCreditLimit": 1234.5,
        "balanceCloseDate": "2026-10-25", "balanceDueDate": "2026-11-05", "holderType": "MAIN",
    })]
    fake.put(pluggy_tx(
        "t1", description="AMAZON", amount=-200, when=date(2026, 10, 5), provider_id="OF-9",
        operation_type="COMPRA", category="Shopping", category_id="08000000",
        merchant={"name": "AMZ", "businessName": "Amazon BR", "cnpj": "15436940000103"},
        payment_type="INSTALLMENT", installment=(1, 4), forecast="2026-11",
        post_date="2026-10-26", transaction_at="2026-10-05T13:22:10.000Z",
        extra={"creditCardMetadata": {
            "installmentNumber": 1, "totalInstallments": 4, "paymentType": "INSTALLMENT",
            "billForecastDate": "2026-11", "billPostDate": "2026-10-26",
            "transactionDateTime": "2026-10-05T13:22:10.000Z", "cardNumber": "1234", "payeeMCC": "5411",
        }},
    ))
    await run_sync(session, pluggy_conn, fake)
    row = await tx_by_ext(session, "t1")
    assert row.provider_id == "OF-9"
    assert row.operation_type == "COMPRA"
    assert row.provider_category == "Shopping" and row.provider_category_id == "08000000"
    assert row.merchant_name == "Amazon BR" and row.merchant_cnpj == "15436940000103"
    assert row.merchant_mcc == "5411" and row.card_number == "1234"
    assert row.payment_type == "INSTALLMENT"
    assert row.bill_forecast_month == date(2026, 11, 1)
    assert row.bill_post_date == date(2026, 10, 26)
    assert row.transaction_at.replace(tzinfo=timezone.utc) == datetime(2026, 10, 5, 13, 22, 10, tzinfo=timezone.utc)
    card = await account_of(session, CARD)
    assert card.available_credit_limit == Decimal("1234.50")
    assert card.balance_close_date == date(2026, 10, 25)
    assert card.card_holder_type == "MAIN"
    conn = await session.get(BankConnection, pluggy_conn.id)
    await session.refresh(conn)
    assert conn.provider_status == "UPDATED"


@pytest.mark.asyncio
async def test_secondary_sweep_failure_degrades(session, pluggy_conn, fake, frozen_today):
    fake.put(pluggy_tx("t1", amount=-10, when=date(2026, 10, 5)))
    await run_sync(session, pluggy_conn, fake)
    fake.fail_created_sweep = True
    fake.put(pluggy_tx("t2", amount=-11, when=date(2026, 10, 6),
                       created_at=datetime(2026, 10, 16, tzinfo=timezone.utc)))
    await run_sync(session, pluggy_conn, fake)  # does not raise
    assert (await tx_by_ext(session, "t2")).amount == Decimal("11")


# --- AC-2.13 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_2_13_other_providers_keep_dedup_and_phantom_cleanup(
    session, test_user, test_workspace
):
    conn = BankConnection(
        user_id=test_user.id, workspace_id=test_workspace.id, provider="simplefin",
        external_id="sf-1", institution_name="Bank", credentials={"token": "x"}, status="active",
    )
    session.add(conn)
    await session.commit()
    mock_provider = AsyncMock()
    from app.providers.base import AccountData

    mock_provider.refresh_credentials = AsyncMock(side_effect=lambda c: c)
    mock_provider.get_institution_logo = AsyncMock(return_value=None)
    mock_provider.get_accounts = AsyncMock(return_value=[
        AccountData(external_id="a1", name="Checking", type="checking", balance=Decimal("0"), currency="USD")
    ])
    mock_provider.get_bills = AsyncMock(return_value=[])
    mock_provider.get_holdings = AsyncMock(return_value=[])
    mock_provider.get_transactions = AsyncMock(return_value=[])
    with patch("app.services.connection_service.get_provider", return_value=mock_provider), \
         patch("app.services.connection_service._cleanup_phantom_duplicates",
               new_callable=AsyncMock, return_value=set()) as cleanup, \
         patch("app.services.connection_service.sync_pluggy_account", new_callable=AsyncMock) as pluggy_path, \
         patch("app.services.connection_service.recompute_accounts", new_callable=AsyncMock) as recompute:
        await connection_service.sync_connection(session, conn.id, conn.workspace_id, test_user.id)
    cleanup.assert_awaited_once()
    pluggy_path.assert_not_awaited()
    recompute.assert_not_awaited()
    mock_provider.get_transactions.assert_awaited()
