"""Soft removal by absence (RFC §4.7).

Without webhooks, a transaction Pluggy deleted is only visible as one that
stopped coming back. After an account sync, stored rows inside the window
that was swept completely (the whole history on a full sync), plus the old
PENDING rows refetched by id, that no sweep returned are marked removed —
reusing ``is_ignored`` so every total drops them, reversibly.

Safety guard: an empty answer, or one that would remove more than 5 rows
and more than 30 % of the window, is much more likely a provider hiccup
than a mass deletion — nothing is marked and a warning is logged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

MAX_REMOVAL_RATIO = 0.3
MIN_REMOVALS_TO_GUARD = 5


@dataclass(frozen=True)
class RemovalCandidate:
    id: object
    external_id: str
    date: date


@dataclass
class RemovalPlan:
    remove: list[object]
    guarded: bool
    window_total: int


def plan_removals(
    candidates: list[RemovalCandidate],
    *,
    returned_ids: set[str],
    full: bool,
    window_from: Optional[date],
    refetched_ids: set[str],
    window_count: int,
) -> RemovalPlan:
    """Which stored rows to mark removed.

    ``candidates`` are the account's live synced rows (not yet removed, with
    a provider id). A row is in scope when the sweep covered it completely:
    any row on a full read, rows dated inside the window otherwise, and rows
    the pending refetch asked for by id.
    """
    in_scope = [
        c for c in candidates
        if full
        or (window_from is not None and c.date >= window_from)
        or c.external_id in refetched_ids
    ]
    stale = [c.id for c in in_scope if c.external_id not in returned_ids]
    if not stale:
        return RemovalPlan(remove=[], guarded=False, window_total=len(in_scope))
    if window_count == 0 or (
        len(stale) > MIN_REMOVALS_TO_GUARD
        and len(stale) / max(len(in_scope), 1) > MAX_REMOVAL_RATIO
    ):
        return RemovalPlan(remove=[], guarded=True, window_total=len(in_scope))
    return RemovalPlan(remove=stale, guarded=False, window_total=len(in_scope))
