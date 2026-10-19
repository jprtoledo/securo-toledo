"""Installment plans in the database (RFC toledo-features §7).

Recompute step 4 materializes the plans of an account and links each parcel:

1. the user's decisions always win (``installment_plan_manual_id`` → manual);
2. manual installment series are plans of their own (``series``), never
   regrouped by the heuristic;
3. every other live parcel is grouped by the heuristic and validated: auto
   when it fits, conflict (kept in the plan, flagged) when it does not;
4. detected plans that lost all parcels are deleted, unless a parcel was
   manually linked to them — then they become manual and keep only those.

Plus the user decisions (link, split, merge, confirm, undo), the plan view
and the parcels a card will still receive (projection).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.installment_plan import InstallmentPlan
from app.models.transaction import Transaction
from app.services import card_statement_service as cards
from app.services.pluggy_sync.provider_update import set_if_changed
from app.services.recompute.installments import (
    ParcelRow,
    derive_plan_fields,
    group_installments,
    is_installment,
    project_plan,
    validate_group,
)
from app.services.recompute.statements import Projection, open_month_for


class PlanDecisionError(ValueError):
    """A user decision that cannot apply (maps to HTTP 400)."""


@dataclass
class PlanSync:
    plans: dict = field(default_factory=dict)  # id → InstallmentPlan
    members: dict = field(default_factory=dict)  # plan id → [Transaction]
    conflicts: int = 0


def _is_live(tx: Transaction) -> bool:
    return (
        tx.provider_removed_at is None
        and tx.ignored_reason != "duplicate"
        and tx.source != "opening_balance"
    )


def _parcel(tx: Transaction, account: Account, rule_set) -> ParcelRow:
    return ParcelRow(
        id=tx.id,
        description=tx.description,
        amount=abs(cards.account_amount(tx, account)),
        installment_number=tx.installment_number,
        total_installments=tx.total_installments,
        month=tx.effective_date.replace(day=1),
        date=tx.date,
        purchase_date=tx.installment_purchase_date,
        payment_type=tx.payment_type,
        strip_patterns=rule_set.strip_patterns(tx) if rule_set is not None else [],
        pending=tx.status == "pending",
    )


async def load_plans(session: AsyncSession, account_id: uuid.UUID) -> list[InstallmentPlan]:
    return list(
        (
            await session.execute(
                select(InstallmentPlan).where(InstallmentPlan.account_id == account_id)
            )
        ).scalars().all()
    )


def _apply_fields(plan: InstallmentPlan, fields: dict) -> None:
    for key, value in fields.items():
        if value is not None or key == "purchase_date":
            set_if_changed(plan, key, value)


async def sync_plans(
    session: AsyncSession, account: Account, rows: list[Transaction], rule_set, changes
) -> PlanSync:
    """Recompute step 4. ``changes`` is the recompute's change tracker."""
    out = PlanSync()
    plans = {p.id: p for p in await load_plans(session, account.id)}
    by_key = {p.key: p for p in plans.values() if p.key}
    live = [tx for tx in rows if _is_live(tx)]
    parcels = {tx.id: _parcel(tx, account, rule_set) for tx in live}

    assignment: dict = {}  # tx id → (plan, link, issue)

    # 1) the user's decisions
    manual_members: dict = {}
    for tx in live:
        plan = plans.get(tx.installment_plan_manual_id) if tx.installment_plan_manual_id else None
        if plan is not None and is_installment(parcels[tx.id]):
            assignment[tx.id] = (plan, "manual", None)
            manual_members.setdefault(plan.id, []).append(tx.id)

    # 2) manual installment series
    series: dict = {}
    for tx in live:
        if tx.id in assignment or tx.installment_series_id is None:
            continue
        if not is_installment(parcels[tx.id]):
            continue
        series.setdefault(tx.installment_series_id, []).append(tx.id)
    seen_keys: set = set()
    for series_id, ids in series.items():
        key = f"series:{series_id}"
        seen_keys.add(key)
        members = [parcels[i] for i in ids]
        fields = derive_plan_fields(members)
        plan = by_key.get(key)
        if plan is None:
            plan = InstallmentPlan(
                workspace_id=account.workspace_id, account_id=account.id, source="series", key=key, **fields,
            )
            session.add(plan)
            await session.flush()
            plans[plan.id] = plan
            by_key[key] = plan
        else:
            _apply_fields(plan, fields)
        for i in ids:
            assignment[i] = (plan, "auto", None)

    # 3) the heuristic, on everything else
    candidates = [
        parcels[tx.id] for tx in live
        if tx.id not in assignment and tx.installment_series_id is None
    ]
    for group in group_installments(candidates):
        seen_keys.add(group.key)
        fields = derive_plan_fields(group.members, group.total_installments)
        fields["first_month"] = group.first_month
        plan = by_key.get(group.key)
        if plan is None:
            plan = InstallmentPlan(
                workspace_id=account.workspace_id, account_id=account.id, source="detected",
                key=group.key, **fields,
            )
            session.add(plan)
            await session.flush()
            plans[plan.id] = plan
            by_key[group.key] = plan
        else:
            _apply_fields(plan, fields)
        for tx_id, issue in validate_group(group.members, group.first_month, group.total_installments).items():
            assignment[tx_id] = (plan, "conflict" if issue else "auto", issue)

    # 4) lifecycle of plans nothing (or only decisions) points at any more
    for plan in list(plans.values()):
        manual_ids = manual_members.get(plan.id)
        if plan.source == "detected" and plan.key not in seen_keys:
            if manual_ids:
                set_if_changed(plan, "source", "manual")
                set_if_changed(plan, "key", None)
            else:
                await _drop_plan(session, plan, rows, changes)
                plans.pop(plan.id)
                continue
        if plan.source == "series" and plan.key not in seen_keys:
            await _drop_plan(session, plan, rows, changes)
            plans.pop(plan.id)
            continue
        if plan.source == "manual":
            if not manual_ids:
                await _drop_plan(session, plan, rows, changes)
                plans.pop(plan.id)
                continue
            _apply_fields(plan, derive_plan_fields([parcels[i] for i in manual_ids]))

    # 5) write the links
    for tx in rows:
        plan, link, issue = assignment.get(tx.id, (None, None, None))
        changes.set(tx, "installment_plan_id", plan.id if plan else None)
        changes.set(tx, "installment_link", link)
        changes.set(tx, "installment_issue", issue)
        if plan is not None:
            out.members.setdefault(plan.id, []).append(tx)
            out.conflicts += link == "conflict"

    # 6) the plan's category: the latest one the user chose on a parcel,
    # unless one was already recorded (the update path records each choice).
    for plan in plans.values():
        if plan.category_id is None:
            chosen = [
                tx for tx in out.members.get(plan.id, [])
                if tx.category_source == "manual" and tx.category_id is not None
            ]
            if chosen:
                latest = max(chosen, key=lambda t: (t.installment_number or 0, t.date))
                set_if_changed(plan, "category_id", latest.category_id)
    out.plans = plans
    return out


async def _drop_plan(session: AsyncSession, plan: InstallmentPlan, rows, changes) -> None:
    for tx in rows:
        if tx.installment_plan_id == plan.id:
            changes.set(tx, "installment_plan_id", None)
            changes.set(tx, "installment_link", None)
            changes.set(tx, "installment_issue", None)
        if tx.installment_plan_manual_id == plan.id:
            changes.set(tx, "installment_plan_manual_id", None)
    await session.flush()
    await session.delete(plan)


# --- the plan view and projection -------------------------------------------


@dataclass
class CardPlanContext:
    account: Account
    plans: list[InstallmentPlan]
    members: dict
    open_month: date
    history_start: Optional[date]


async def load_card_plan_context(
    session: AsyncSession, account: Account, *, today: date
) -> CardPlanContext:
    rows = list(
        (await session.execute(select(Transaction).where(Transaction.account_id == account.id))).scalars().all()
    )
    bills, _, _ = cards.known_bills(await cards.load_bills(session, account.id), rows)
    cycle = cards.cycle_for(account, bills)
    open_month = open_month_for(today, {b.month: b for b in bills}, cycle)
    live = [tx for tx in rows if not tx.is_ignored and tx.source != "opening_balance"]
    history_start = min((tx.effective_date.replace(day=1) for tx in live), default=None)
    plans = await load_plans(session, account.id)
    members: dict = {}
    for tx in rows:
        if tx.installment_plan_id is not None:
            members.setdefault(tx.installment_plan_id, []).append(tx)
    return CardPlanContext(account, plans, members, open_month, history_start)


def planned_parcels(ctx: CardPlanContext, plan: InstallmentPlan):
    known = {tx.installment_number for tx in ctx.members.get(plan.id, []) if tx.installment_number}
    return project_plan(
        total_installments=plan.total_installments,
        first_month=plan.first_month,
        installment_amount=Decimal(plan.installment_amount),
        known_numbers=known,
        open_month=ctx.open_month,
        history_start=ctx.history_start,
    )


async def card_installment_projections(
    session: AsyncSession, account: Account, *, today: date
) -> list[Projection]:
    ctx = await load_card_plan_context(session, account, today=today)
    return [
        Projection(p.month, p.amount, "installment", {
            "description": plan.description,
            "number": p.number,
            "total": plan.total_installments,
            "ref_id": str(plan.id),
            "category_id": str(plan.category_id) if plan.category_id else None,
        })
        for plan in ctx.plans
        for p in planned_parcels(ctx, plan)
        if p.kind == "projected"
    ]


async def plan_view(session: AsyncSession, account: Account, *, today: date) -> dict:
    """GET /api/accounts/{id}/installment-plans (RFC §7.3)."""
    from app.services.recompute.months import add_months

    ctx = await load_card_plan_context(session, account, today=today)
    plans_out = []
    total_conflicts = total_missing = 0
    for plan in ctx.plans:
        members = sorted(ctx.members.get(plan.id, []), key=lambda t: (t.installment_number or 0, t.date))
        installments = [
            {
                "number": tx.installment_number,
                "month": tx.effective_date.replace(day=1),
                "amount": abs(cards.account_amount(tx, account)),
                "kind": "posted",
                "transaction_id": tx.id,
                "pending": tx.status == "pending",
                "link": tx.installment_link,
                "issue": tx.installment_issue,
            }
            for tx in members
        ]
        planned = planned_parcels(ctx, plan)
        installments += [
            {
                "number": p.number, "month": p.month, "amount": p.amount, "kind": p.kind,
                "transaction_id": None, "pending": False, "link": None, "issue": None,
            }
            for p in planned
        ]
        installments.sort(key=lambda i: (i["number"] or 0, i["kind"] != "posted"))
        conflicts = sum(1 for tx in members if tx.installment_link == "conflict")
        missing = sum(1 for p in planned if p.kind == "missing")
        posted_numbers = [tx.installment_number for tx in members if tx.installment_number]
        current = max(posted_numbers, default=0)
        last_month = add_months(plan.first_month, plan.total_installments - 1)
        remaining = sum((p.amount for p in planned if p.kind == "projected"), Decimal("0"))
        total_conflicts += conflicts
        total_missing += missing
        plans_out.append({
            "id": plan.id,
            "source": plan.source,
            "description": plan.description,
            "purchase_date": plan.purchase_date,
            "total_installments": plan.total_installments,
            "installment_amount": plan.installment_amount,
            "total_amount": (Decimal(plan.installment_amount) * plan.total_installments).quantize(Decimal("0.01")),
            "first_month": plan.first_month,
            "last_month": last_month,
            "current_installment": current,
            "remaining": remaining,
            "active": last_month >= ctx.open_month,
            "conflicts": conflicts,
            "missing": missing,
            "category_id": plan.category_id,
            "installments": installments,
        })
    plans_out.sort(key=lambda p: (not p["active"], -p["first_month"].toordinal()))
    return {
        "open_month": ctx.open_month,
        "alerts": {"conflicts": total_conflicts, "missing": total_missing},
        "plans": plans_out,
    }


# --- user decisions (RFC §7.2) ---------------------------------------------------


async def _plan_in_account(session, plan_id, account_id) -> InstallmentPlan:
    plan = await session.get(InstallmentPlan, plan_id)
    if plan is None or plan.account_id != account_id:
        raise PlanDecisionError("Installment plan not found")
    return plan


async def _members(session, plan_id) -> list[Transaction]:
    return list(
        (
            await session.execute(
                select(Transaction).where(
                    (Transaction.installment_plan_id == plan_id)
                    | (Transaction.installment_plan_manual_id == plan_id)
                )
            )
        ).scalars().all()
    )


async def link_parcel(session: AsyncSession, tx: Transaction, plan_id) -> None:
    """Link a parcel to a plan (``plan_id``), to a new plan of its own
    (``"new"``) or back to the heuristic (``None``, undo)."""
    if plan_id is None:
        tx.installment_plan_manual_id = None
        return
    if not tx.total_installments or not tx.installment_number:
        raise PlanDecisionError("This transaction is not an installment")
    if plan_id == "new":
        account = await session.get(Account, tx.account_id)
        plan = InstallmentPlan(
            workspace_id=tx.workspace_id,
            account_id=tx.account_id,
            source="manual",
            description=tx.description[:500],
            total_installments=tx.total_installments,
            installment_amount=abs(cards.account_amount(tx, account)),
            first_month=(tx.effective_date.replace(day=1)),
            purchase_date=tx.installment_purchase_date,
        )
        session.add(plan)
        await session.flush()
        tx.installment_plan_manual_id = plan.id
        return
    plan = await _plan_in_account(session, uuid.UUID(str(plan_id)), tx.account_id)
    if plan.total_installments != tx.total_installments:
        raise PlanDecisionError("The plan has a different number of installments")
    taken = {
        m.installment_number for m in await _members(session, plan.id)
        if m.id != tx.id and m.installment_number
    }
    if tx.installment_number in taken:
        raise PlanDecisionError(f"The plan already has installment {tx.installment_number}")
    tx.installment_plan_manual_id = plan.id


async def confirm_parcel(session: AsyncSession, tx: Transaction) -> None:
    """A conflict the user accepts: manual link to its current plan."""
    if tx.installment_plan_id is None:
        raise PlanDecisionError("This transaction is not in an installment plan")
    tx.installment_plan_manual_id = tx.installment_plan_id


async def merge_plans(session: AsyncSession, plan: InstallmentPlan, into_plan_id) -> InstallmentPlan:
    target = await _plan_in_account(session, into_plan_id, plan.account_id)
    if target.id == plan.id:
        raise PlanDecisionError("Cannot merge a plan into itself")
    if target.total_installments != plan.total_installments:
        raise PlanDecisionError("The plans have a different number of installments")
    source_members = await _members(session, plan.id)
    target_numbers = {m.installment_number for m in await _members(session, target.id)}
    overlap = sorted({m.installment_number for m in source_members} & target_numbers - {None})
    if overlap:
        raise PlanDecisionError(
            "Both plans have installment " + ", ".join(str(n) for n in overlap)
        )
    for tx in source_members:
        tx.installment_plan_manual_id = target.id
    return target


async def record_category_choice(session: AsyncSession, tx: Transaction) -> None:
    """The user picked a category on a parcel: the plan follows it, and so
    do the plan's parcels that arrive later (RFC §7.2, AC-5.10)."""
    if tx.installment_plan_id is None:
        return
    plan = await session.get(InstallmentPlan, tx.installment_plan_id)
    if plan is not None and tx.category_source == "manual":
        plan.category_id = tx.category_id
