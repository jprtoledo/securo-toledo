"""Which Pluggy transactions to read for one account (RFC §4.4).

A first sync reads the account's whole history. An incremental sync reads
the union of three sweeps, de-duplicated by id:

1. the date window — ``dateFrom = today − PLUGGY_SYNC_LOOKBACK_DAYS`` —
   catches status and amount changes on recent rows;
2. the ingestion window — ``createdAtFrom = watermark − 24 h`` — catches
   rows Pluggy ingested late with an old date (monthly parcels, settled
   merchants);
3. a refetch by id of the stored PENDING rows older than the window,
   which Pluggy only re-collects for 7 days: a PENDING→POSTED change on
   them would otherwise never arrive.

Sweeps 2 and 3 degrade to a warning on failure; the date window (or the
full read) failing fails the sync, as before.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from app.providers.base import TransactionData

logger = logging.getLogger(__name__)

CREATED_AT_OVERLAP = timedelta(hours=24)
IDS_PER_REQUEST = 500


@dataclass(frozen=True)
class SweepPlan:
    full: bool
    window_from: Optional[date]
    created_at_from: Optional[datetime]


def plan_sweeps(
    watermark: Optional[datetime], today: date, lookback_days: int
) -> SweepPlan:
    if watermark is None:
        return SweepPlan(full=True, window_from=None, created_at_from=None)
    return SweepPlan(
        full=False,
        window_from=today - timedelta(days=lookback_days),
        created_at_from=watermark - CREATED_AT_OVERLAP,
    )


def union_by_id(*sweeps: Iterable[TransactionData]) -> list[TransactionData]:
    """Merge sweeps, one entry per external id. When an id comes back in
    several sweeps the most recently updated payload wins (ties: the later
    sweep), so a stale copy never overwrites a fresher one."""
    by_id: dict[str, TransactionData] = {}
    for sweep in sweeps:
        for txn in sweep:
            current = by_id.get(txn.external_id)
            if current is None:
                by_id[txn.external_id] = txn
                continue
            a, b = current.provider_updated_at, txn.provider_updated_at
            if a is not None and b is not None and b < a:
                continue
            by_id[txn.external_id] = txn
    return list(by_id.values())


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


@dataclass
class SweepResult:
    transactions: list[TransactionData]
    plan: SweepPlan
    # Every id the provider returned, by any sweep.
    returned_ids: set[str]
    # Ids asked for by the pending refetch (absent ones count as removed).
    refetched_ids: set[str] = field(default_factory=set)
    # Rows returned by the full read / date window (the removal guard's
    # "the account returned nothing" signal).
    window_count: int = 0
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


async def fetch_account(
    provider,
    credentials: dict,
    account_external_id: str,
    *,
    payee_source: str,
    plan: SweepPlan,
    pending_ids: list[str],
) -> SweepResult:
    """Run the sweeps of ``plan`` against ``provider`` (a PluggyProvider)."""
    warnings: list[str] = []
    counts: dict[str, int] = {}

    if plan.full:
        # The plain interface without ``since`` is the unfiltered read.
        primary = await provider.get_transactions(
            credentials, account_external_id, None, payee_source=payee_source
        )
        counts["full"] = len(primary)
        merged = union_by_id(primary)
        return SweepResult(
            transactions=merged,
            plan=plan,
            returned_ids={t.external_id for t in merged},
            window_count=len(primary),
            counts=counts,
        )

    by_date = await provider.get_transactions_filtered(
        credentials,
        account_external_id,
        payee_source=payee_source,
        date_from=plan.window_from,
    )
    counts["date_window"] = len(by_date)

    by_created: list[TransactionData] = []
    try:
        by_created = await provider.get_transactions_filtered(
            credentials,
            account_external_id,
            payee_source=payee_source,
            created_at_from=plan.created_at_from.isoformat() if plan.created_at_from else None,
        )
    except Exception as exc:  # noqa: BLE001 — a secondary sweep never fails the sync
        warnings.append(f"ingestion-window sweep failed: {exc}")
        logger.warning(
            "Pluggy createdAtFrom sweep failed for account %s", account_external_id, exc_info=True
        )
    counts["created_since"] = len(by_created)

    refetched: list[TransactionData] = []
    asked: set[str] = set()
    for batch in chunked(pending_ids, IDS_PER_REQUEST):
        try:
            refetched.extend(
                await provider.get_transactions_filtered(
                    credentials,
                    account_external_id,
                    payee_source=payee_source,
                    ids=batch,
                )
            )
            asked.update(batch)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"pending refetch failed: {exc}")
            logger.warning(
                "Pluggy pending refetch failed for account %s", account_external_id, exc_info=True
            )
            break
    counts["pending_refetch"] = len(refetched)

    merged = union_by_id(by_date, by_created, refetched)
    return SweepResult(
        transactions=merged,
        plan=plan,
        returned_ids={t.external_id for t in merged},
        refetched_ids=asked,
        window_count=len(by_date),
        warnings=warnings,
        counts=counts,
    )
