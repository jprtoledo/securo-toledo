"""Per-account Pluggy transaction sync (RFC toledo-features §4.4–§4.7).

Replaces, for Pluggy accounts only, the generic transaction loop of
``connection_service.sync_connection``:

1. fetch the union of sweeps (``sweeps``);
2. match each incoming row: same ``external_id`` → same row; same
   ``provider_id`` under another id → same row, re-keyed; then the passes
   that target non-sync rows (fuzzy manual match, local-import history,
   recurring placeholder); otherwise insert. Pending↔posted and installment
   twins are no longer collapsed here — recompute flags them as duplicates;
3. existing rows follow the bank (``provider_update``);
4. rows that stopped coming back are soft-removed (``removals``);
5. bills referenced by rows but missing from /bills are fetched by id;
6. the account's ingestion watermark advances.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today
from app.core.config import get_settings
from app.models.account import Account
from app.models.credit_card_bill import CreditCardBill
from app.models.transaction import Transaction
from app.providers.base import AccountData, TransactionData
from app.services.credit_card_service import apply_effective_date
from app.services.fx_rate_service import stamp_primary_amount
from app.services.pluggy_sync import provider_update
from app.services.pluggy_sync.removals import RemovalCandidate, plan_removals
from app.services.pluggy_sync.sweeps import fetch_account, plan_sweeps

logger = logging.getLogger(__name__)

SYNTHETIC_PREFIX = "bill_charge:"


@dataclass
class PluggyAccountSync:
    merged: int = 0
    inserted: int = 0
    updated: int = 0
    rekeyed: int = 0
    restored: int = 0
    removed: int = 0
    missing_bills: int = 0
    returned_ids: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def _restamp_fx(
    session: AsyncSession,
    tx: Transaction,
    data: TransactionData,
    *,
    user_id: uuid.UUID,
    acct_currency: str,
    user_currency: str,
) -> None:
    """Same rule as the insert path: prefer the bank's own conversion."""
    if (
        data.amount_in_account_currency is not None
        and data.amount
        and acct_currency == user_currency
        and data.currency != acct_currency
    ):
        provider_update.set_if_changed(tx, "amount_primary", data.amount_in_account_currency)
        provider_update.set_if_changed(
            tx, "fx_rate_used", data.amount_in_account_currency / data.amount
        )
    else:
        await stamp_primary_amount(session, user_id, tx)


def _place(tx: Transaction, account: Account, bills_by_id: dict) -> None:
    """Re-resolve the effective date after the date or the bill moved
    (Phase 4's recompute takes this over for credit cards)."""
    bill = bills_by_id.get(tx.bill_id) if tx.bill_id else None
    apply_effective_date(tx, account, bill_due_date=bill.due_date if bill else None)


async def _load_rows(session: AsyncSession, account_id: uuid.UUID) -> list[Transaction]:
    return list(
        (
            await session.execute(
                select(Transaction)
                .where(Transaction.account_id == account_id)
                .order_by(Transaction.created_at)
            )
        ).scalars().all()
    )


async def sync_pluggy_account(
    session: AsyncSession,
    *,
    provider,
    credentials: dict,
    account: Account,
    acc_data: AccountData,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_currency: str,
    payee_source: str,
    import_pending: bool,
    use_provider_cats: bool,
    bills_by_external_id: dict[str, CreditCardBill],
    new_tx_ids: list[uuid.UUID],
) -> PluggyAccountSync:
    # Local import: connection_service imports this module, and the generic
    # helpers reused below live there.
    from app.services import connection_service as cs
    from app.services.transaction_match_service import find_unique_transaction_match

    out = PluggyAccountSync()
    settings = get_settings()
    plan = plan_sweeps(
        _aware(account.provider_tx_created_watermark),
        app_today(),
        settings.pluggy_sync_lookback_days,
    )

    stored = await _load_rows(session, account.id)
    pending_ids = (
        []
        if plan.full
        else [
            tx.external_id
            for tx in stored
            if tx.status == "pending"
            and tx.source == "sync"
            and tx.external_id
            and not tx.external_id.startswith(SYNTHETIC_PREFIX)
            and tx.provider_removed_at is None
            and plan.window_from is not None
            and tx.date < plan.window_from
        ]
    )

    sweep = await fetch_account(
        provider,
        credentials,
        acc_data.external_id,
        payee_source=payee_source,
        plan=plan,
        pending_ids=pending_ids,
    )
    out.returned_ids = sweep.returned_ids
    out.warnings.extend(sweep.warnings)

    incoming = sweep.transactions
    if not import_pending:
        incoming = [t for t in incoming if t.status != "pending"]
    incoming_ids = {t.external_id for t in incoming}

    # The oldest row wins when a past race left two rows on one id (the
    # generic loop does the same with .first()).
    by_external: dict[str, Transaction] = {}
    by_provider_id: dict[str, Transaction] = {}
    for tx in stored:
        if tx.external_id and tx.external_id not in by_external:
            by_external[tx.external_id] = tx
        if tx.provider_id and tx.provider_id not in by_provider_id:
            by_provider_id[tx.provider_id] = tx
    bills_by_id = {b.id: b for b in bills_by_external_id.values()}
    acct_currency = acc_data.currency or user_currency

    for data in incoming:
        currency = data.currency or acc_data.currency or user_currency
        bill = bills_by_external_id.get(data.bill_external_id) if data.bill_external_id else None

        row = by_external.get(data.external_id)
        if row is None and data.provider_id:
            candidate = by_provider_id.get(data.provider_id)
            # Re-key only a row whose own id did not come back: two live
            # ids sharing a providerId are two rows, not one renamed row.
            if (
                candidate is not None
                and candidate.external_id not in sweep.returned_ids
                and candidate.source == "sync"
            ):
                by_external.pop(candidate.external_id or "", None)
                candidate.external_id = data.external_id
                by_external[data.external_id] = candidate
                row = candidate
                out.rekeyed += 1

        if row is not None:
            outcome = provider_update.update_existing_row(row, data, currency=currency, bill=bill)
            if outcome.restored:
                out.restored += 1
            if outcome.money_changed:
                await _restamp_fx(
                    session, row, data,
                    user_id=user_id, acct_currency=acct_currency, user_currency=user_currency,
                )
            if outcome.placement_changed:
                _place(row, account, bills_by_id)
            if outcome.changed:
                out.updated += 1
            continue

        # Passes that adopt rows Securo did not get from this sync.
        fuzzy = await cs._fuzzy_match_manual(session, account.id, data)
        if fuzzy is not None:
            if not fuzzy.is_ignored:
                cs._merge_sync_metadata(fuzzy, data)
                fuzzy.source = "sync"
                provider_update.apply_provider_columns(fuzzy, data)
                by_external[data.external_id] = fuzzy
                out.merged += 1
            continue

        imported = await find_unique_transaction_match(
            session,
            account.id,
            data,
            cs.LOCAL_IMPORT_SOURCES,
            unclaimed_only=True,
            exclude_external_ids=incoming_ids,
        )
        if imported is not None:
            if imported.original_description is None:
                imported.original_description = data.description
            cs._merge_sync_metadata(imported, data)
            provider_update.apply_provider_columns(imported, data)
            by_external[data.external_id] = imported
            continue

        result, landed = await cs._insert_synced_transaction(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            account=account,
            acc_data=acc_data,
            txn_data=data,
            bills_by_external_id=bills_by_external_id,
            user_currency=user_currency,
            use_provider_cats=use_provider_cats,
            new_tx_ids=new_tx_ids,
        )
        if landed is not None and result != "skipped":
            provider_update.apply_provider_columns(landed, data)
            by_external[data.external_id] = landed
        if result == "merged":
            out.merged += 1
        elif result == "inserted":
            out.inserted += 1

    await session.flush()

    # --- removal by absence -------------------------------------------------
    now = datetime.now(timezone.utc)
    rows = await _load_rows(session, account.id)
    candidates = [
        RemovalCandidate(id=tx.id, external_id=tx.external_id, date=tx.date)
        for tx in rows
        if tx.source == "sync"
        and tx.external_id
        and not tx.external_id.startswith(SYNTHETIC_PREFIX)
        and tx.provider_removed_at is None
        and not tx.sync_locked
    ]
    removal = plan_removals(
        candidates,
        returned_ids=sweep.returned_ids,
        full=plan.full,
        window_from=plan.window_from,
        refetched_ids=sweep.refetched_ids,
        window_count=sweep.window_count,
    )
    if removal.guarded:
        message = (
            f"account {account.id}: rows missing from Pluggy exceed the safety "
            f"limit (window of {removal.window_total}); nothing marked removed"
        )
        out.warnings.append(message)
        logger.warning("Pluggy removal guard: %s", message)
    by_id = {tx.id: tx for tx in rows}
    for tx_id in removal.remove:
        tx = by_id[tx_id]
        tx.provider_removed_at = now
        if not provider_update.is_user_ignored(tx):
            tx.is_ignored = True
            tx.ignored_reason = "provider_removed"
        out.removed += 1

    # --- bills referenced but not listed -------------------------------------
    if account.type == "credit_card":
        missing = sorted(
            {
                tx.provider_bill_external_id
                for tx in rows
                if tx.provider_bill_external_id
                and tx.provider_bill_external_id not in bills_by_external_id
            }
        )
        fetched = []
        for bill_external_id in missing:
            try:
                bill_data = await provider.get_bill(credentials, bill_external_id)
            except Exception as exc:  # noqa: BLE001 — best effort, logged
                out.warnings.append(f"bill {bill_external_id} not found: {exc}")
                logger.info("Pluggy bill %s could not be fetched: %s", bill_external_id, exc)
                continue
            if bill_data is not None:
                fetched.append(bill_data)
        if fetched:
            stored_bills = await cs._upsert_credit_card_bills(session, user_id, account, fetched)
            bills_by_external_id.update(stored_bills)
            bills_by_id = {b.id: b for b in bills_by_external_id.values()}
            out.missing_bills = len(fetched)
            for tx in rows:
                if (
                    tx.provider_bill_external_id in stored_bills
                    and tx.effective_bill_date is None
                    and not provider_update.is_frozen(tx)
                ):
                    bill = stored_bills[tx.provider_bill_external_id]
                    if provider_update.set_if_changed(tx, "bill_id", bill.id):
                        _place(tx, account, bills_by_id)

    # --- watermark -----------------------------------------------------------
    seen = [
        _aware(t.provider_created_at) for t in sweep.transactions if t.provider_created_at
    ] + [_aware(tx.provider_created_at) for tx in rows if tx.provider_created_at]
    if seen:
        watermark = max(seen)
        current = _aware(account.provider_tx_created_watermark)
        if current is None or watermark > current:
            account.provider_tx_created_watermark = watermark

    await session.flush()
    return out
