import calendar
import uuid
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_clock import app_today, get_workspace_timezone, today_in
from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.recurring_transaction import RecurringTransaction
from app.models.transaction import Transaction
from app.schemas.recurring_transaction import RecurringTransactionCreate, RecurringTransactionUpdate
from app.services import recurring_match_service
from app.services.credit_card_service import apply_effective_date
from app.services.fx_rate_service import stamp_primary_amount


async def _verify_account_in_workspace(
    session: AsyncSession, workspace_id: uuid.UUID, account_id: uuid.UUID
) -> Account:
    """Raise ValueError if the account isn't reachable from this workspace."""
    result = await session.execute(
        select(Account)
        .outerjoin(BankConnection)
        .where(
            Account.id == account_id,
            or_(
                Account.workspace_id == workspace_id,
                BankConnection.workspace_id == workspace_id,
            ),
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise ValueError("Account not found")
    return account


V2_FIELDS = (
    "match_condition", "amount_mode", "amount_tolerance_pct", "day_window",
    "billing_weekday", "week_count_basis",
)


async def _validate_v2(
    session: AsyncSession, account_id: Optional[uuid.UUID], values: dict
) -> None:
    """Recurrences v2 inputs (RFC toledo-features §8.2): the condition must be
    valid in the rules language for the account's kind, and weekly billed
    monthly needs its weekday and day."""
    from app.services.rule_engine import account_kind_of, rule_definition_errors

    condition = values.get("match_condition")
    if condition:
        account = await session.get(Account, account_id) if account_id else None
        kind = account_kind_of(account.type) if account else None
        errors = rule_definition_errors(
            kind if kind in ("bank", "credit_card") else None, "and", [condition], []
        )
        if errors:
            raise ValueError(errors[0]["message"])
    if values.get("frequency") == "weekly_billed_monthly":
        if values.get("billing_weekday") is None:
            raise ValueError("billing_weekday is required for weekly billed monthly")
        if not values.get("day_of_month"):
            raise ValueError("day_of_month (the billing day) is required for weekly billed monthly")


async def _validate_extra_accounts(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    account_id: Optional[uuid.UUID],
    extra_ids: Optional[list],
    *,
    auto_generate: bool,
    condition: Optional[dict],
) -> Optional[list[str]]:
    """Other accounts that may pay the recurrence: in the workspace, not
    its own account, and only on computed recurrences (the placeholder
    generator works on one account). Returns the stored form."""
    from app.services.account_service import get_account_name
    from app.services.rule_engine import account_kind_of, rule_definition_errors

    ids = list(dict.fromkeys(uuid.UUID(str(a)) for a in extra_ids or [] if uuid.UUID(str(a)) != account_id))
    if not ids:
        return None
    if auto_generate:
        raise ValueError("Only recurrences that are not auto-generated can be paid from several accounts")
    for extra_id in ids:
        account = await _verify_account_in_workspace(session, workspace_id, extra_id)
        if condition:
            kind = account_kind_of(account.type)
            errors = rule_definition_errors(
                kind if kind in ("bank", "credit_card") else None, "and", [condition], []
            )
            if errors:
                raise ValueError(f"{get_account_name(account)}: {errors[0]['message']}")
    return [str(a) for a in ids]


async def recompute_recurrence_account(
    session: AsyncSession, account_id: Optional[uuid.UUID]
) -> Optional[dict]:
    """Every recurrence change re-links its account (RFC §8.7); returns the
    link stats."""
    if account_id is None:
        return None
    from app.services.recompute_service import recompute_account

    stats = await recompute_account(session, account_id)
    await session.commit()
    return {
        k: stats.extra.get(k, 0) for k in ("recurrences", "linked", "recurring_conflicts")
    }


async def get_recurring_transactions(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[RecurringTransaction]:
    result = await session.execute(
        select(RecurringTransaction)
        .where(RecurringTransaction.workspace_id == workspace_id)
        .order_by(RecurringTransaction.next_occurrence)
    )
    return list(result.scalars().all())


async def get_recurring_transaction(
    session: AsyncSession, recurring_id: uuid.UUID, workspace_id: uuid.UUID
) -> Optional[RecurringTransaction]:
    result = await session.execute(
        select(RecurringTransaction)
        .where(RecurringTransaction.id == recurring_id, RecurringTransaction.workspace_id == workspace_id)
    )
    return result.scalar_one_or_none()


async def create_recurring_transaction(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    data: RecurringTransactionCreate,
) -> RecurringTransaction:
    await _verify_account_in_workspace(session, workspace_id, data.account_id)
    await _validate_v2(session, data.account_id, data.model_dump())
    extra_account_ids = await _validate_extra_accounts(
        session, workspace_id, data.account_id, data.extra_account_ids,
        auto_generate=data.auto_generate, condition=data.match_condition,
    )
    next_occ = data.start_date
    if data.skip_first:
        next_occ = _advance_date(
            data.start_date, data.frequency,
            intended_day=data.day_of_month or data.start_date.day,
        )
    recurring = RecurringTransaction(
        user_id=user_id,
        workspace_id=workspace_id,
        account_id=data.account_id,
        category_id=data.category_id,
        description=data.description,
        amount=data.amount,
        currency=data.currency,
        type=data.type,
        frequency=data.frequency,
        weekend_adjustment=data.weekend_adjustment,
        day_of_month=data.day_of_month,
        start_date=data.start_date,
        end_date=data.end_date,
        auto_generate=data.auto_generate,
        next_occurrence=next_occ,
        match_condition=data.match_condition,
        amount_mode=data.amount_mode or "fixed",
        amount_tolerance_pct=data.amount_tolerance_pct or 0,
        day_window=data.day_window,
        billing_weekday=data.billing_weekday,
        week_count_basis=(
            data.week_count_basis or "previous_month"
            if data.frequency == "weekly_billed_monthly" else data.week_count_basis
        ),
        origin=getattr(data, "origin", None) or "manual",
        extra_account_ids=extra_account_ids,
    )
    session.add(recurring)
    await session.flush()
    await stamp_primary_amount(
        session, user_id, recurring,
        date_field="start_date",
    )
    await session.commit()
    await session.refresh(recurring)
    recurring.link_stats = await recompute_recurrence_account(session, recurring.account_id)
    await session.refresh(recurring)
    return recurring


async def update_recurring_transaction(
    session: AsyncSession, recurring_id: uuid.UUID, workspace_id: uuid.UUID, data: RecurringTransactionUpdate
) -> Optional[RecurringTransaction]:
    recurring = await get_recurring_transaction(session, recurring_id, workspace_id)
    if not recurring:
        return None

    update_data = data.model_dump(exclude_unset=True)

    for required in ("weekend_adjustment", "start_date", "frequency"):
        if required in update_data and update_data[required] is None:
            raise ValueError(f"{required} is required")

    # A recurring transaction must always have an account — reject an explicit
    # null, and verify ownership of any new account_id.
    if "account_id" in update_data:
        new_account_id = update_data["account_id"]
        if new_account_id is None:
            raise ValueError("account_id is required")
        if new_account_id != recurring.account_id:
            await _verify_account_in_workspace(session, workspace_id, new_account_id)

    schedule_changed = any(
        key in update_data and update_data[key] != getattr(recurring, key)
        for key in _SCHEDULE_FIELDS
    )
    previous_next_occurrence = recurring.next_occurrence
    previous_account_id = recurring.account_id
    previous_extra_ids = set(recurring.extra_account_ids or [])
    extra_touched = "extra_account_ids" in update_data
    extra_ids = update_data.pop("extra_account_ids", recurring.extra_account_ids)
    if extra_touched or any(k in update_data for k in ("account_id", "auto_generate", "match_condition")):
        recurring.extra_account_ids = await _validate_extra_accounts(
            session, workspace_id, update_data.get("account_id", recurring.account_id), extra_ids,
            auto_generate=update_data.get("auto_generate", recurring.auto_generate),
            condition=update_data.get("match_condition", recurring.match_condition),
        )
    if any(k in update_data for k in (*V2_FIELDS, "frequency", "day_of_month")):
        merged = {
            k: update_data.get(k, getattr(recurring, k))
            for k in (*V2_FIELDS, "frequency", "day_of_month")
        }
        await _validate_v2(session, update_data.get("account_id", recurring.account_id), merged)

    for key, value in update_data.items():
        if key == "amount_tolerance_pct" and value is None:
            value = 0
        if key == "amount_mode" and value is None:
            value = "fixed"
        setattr(recurring, key, value)

    if schedule_changed:
        recurring.next_occurrence = _first_occurrence_on_or_after(
            recurring.start_date,
            recurring.frequency,
            intended_day=recurring.day_of_month or recurring.start_date.day,
            floor=previous_next_occurrence,
        )

    await session.commit()
    await session.refresh(recurring)
    # Accounts that no longer pay it drop their links.
    dropped = {previous_account_id, *(uuid.UUID(a) for a in previous_extra_ids)} - {
        recurring.account_id, *(uuid.UUID(a) for a in recurring.extra_account_ids or []),
    }
    for account_id in dropped:
        await recompute_recurrence_account(session, account_id)
    recurring.link_stats = await recompute_recurrence_account(session, recurring.account_id)
    await session.refresh(recurring)
    return recurring


async def end_recurring_transaction(
    session: AsyncSession, recurring_id: uuid.UUID, workspace_id: uuid.UUID, end: Optional[date]
) -> Optional[RecurringTransaction]:
    """"End recurrence" (RFC §8.5): ``end`` or, by default, the last missed
    period — the one that made Securo suggest ending it."""
    recurring = await get_recurring_transaction(session, recurring_id, workspace_id)
    if not recurring:
        return None
    if end is None:
        from app.services.recurrence_service import timeline_for

        view = await timeline_for(session, recurring)
        end = view.alerts.get("last_missed") or app_today()
    recurring.end_date = end
    await session.commit()
    recurring.link_stats = await recompute_recurrence_account(session, recurring.account_id)
    await session.refresh(recurring)
    return recurring


async def skip_recurring_period(
    session: AsyncSession, recurring_id: uuid.UUID, workspace_id: uuid.UUID, period: date, skipped: bool = True,
) -> Optional[RecurringTransaction]:
    """Mark one occurrence as not happening (or undo it): reports and
    forecasts count every occurrence still to come, except skipped ones."""
    from app.services.recompute.recurrences import periods_of
    from app.services.recurrence_service import timeline_for, to_config

    recurring = await get_recurring_transaction(session, recurring_id, workspace_id)
    if not recurring:
        return None
    if recurring.auto_generate or recurring.account_id is None:
        raise ValueError("Only computed recurrences with an account can skip an occurrence")
    if period not in periods_of(to_config(recurring), recurring.start_date, period):
        raise ValueError(f"{period.isoformat()} is not an occurrence of this recurrence")
    current = set(recurring.skipped_periods or [])
    if skipped:
        view = await timeline_for(session, recurring)
        if any(s.period == period and s.transaction_ids for s in view.timeline):
            raise ValueError("This occurrence already has a linked transaction")
        current.add(period.isoformat())
    else:
        current.discard(period.isoformat())
    # A new list, so the JSON column is flagged as changed.
    recurring.skipped_periods = sorted(current) or None
    await session.commit()
    recurring.link_stats = await recompute_recurrence_account(session, recurring.account_id)
    await session.refresh(recurring)
    return recurring


async def delete_recurring_transaction(
    session: AsyncSession, recurring_id: uuid.UUID, workspace_id: uuid.UUID
) -> bool:
    recurring = await get_recurring_transaction(session, recurring_id, workspace_id)
    if not recurring:
        return False

    account_ids = [recurring.account_id, *(uuid.UUID(a) for a in recurring.extra_account_ids or [])]
    await session.delete(recurring)
    await session.commit()
    for account_id in account_ids:
        await recompute_recurrence_account(session, account_id)
    return True


def _advance_months(current: date, months: int, intended_day: int) -> date:
    """Advance by calendar months, clamping only in a shorter target month."""
    month_index = current.month - 1 + months
    year = current.year + month_index // 12
    month = month_index % 12 + 1
    day = min(intended_day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _advance_date(
    current: date, frequency: str, intended_day: Optional[int] = None,
) -> date:
    """Advance a date by the given frequency.

    For monthly, quarterly, semiannual, and yearly recurrences, ``intended_day`` is the day
    the user actually wants (e.g. 31). We cap it to the target month's length
    so short months clamp, but subsequent occurrences recover to the intended
    day when it exists again. Falls back to ``current.day`` when not provided.
    """
    if frequency == "weekly":
        return current + timedelta(weeks=1)
    if frequency == "biweekly":
        return current + timedelta(weeks=2)

    target_day = intended_day if intended_day else current.day
    if frequency == "monthly":
        return _advance_months(current, 1, target_day)
    if frequency == "quarterly":
        return _advance_months(current, 3, target_day)
    if frequency == "semiannual":
        return _advance_months(current, 6, target_day)
    if frequency == "yearly":
        year = current.year + 1
        day = min(target_day, calendar.monthrange(year, current.month)[1])
        return date(year, current.month, day)

    # Preserve the existing monthly fallback for unknown legacy values.
    return _advance_months(current, 1, target_day)


# Fields the occurrence schedule is derived from; changing any of them moves
# the next_occurrence pointer.
_SCHEDULE_FIELDS = ("start_date", "day_of_month", "frequency")


def _first_occurrence_on_or_after(
    start: date, frequency: str, intended_day: Optional[int], floor: date,
) -> date:
    """Return the first occurrence of the schedule on or after ``floor``.

    The schedule starts at ``start``, as on creation. ``floor`` is the pointer
    before the edit: every occurrence before it was already generated or
    matched, so the new pointer never moves behind it. That keeps an edit from
    backfilling past periods or repeating one that was already charged, while a
    ``start`` later than ``floor`` still defers the rule to ``start``.
    """
    current = start
    while current < floor:
        current = _advance_date(current, frequency, intended_day=intended_day)
    return current


def adjust_weekend_date(
    nominal_date: date, weekend_adjustment: str = "none"
) -> date:
    """Return the effective date without changing the nominal schedule date."""
    if weekend_adjustment not in ("none", "previous_friday", "next_monday"):
        raise ValueError(f"Unsupported weekend adjustment: {weekend_adjustment}")

    weekday = nominal_date.weekday()
    if weekend_adjustment == "none" or weekday < calendar.SATURDAY:
        return nominal_date
    if weekend_adjustment == "previous_friday":
        return nominal_date - timedelta(days=weekday - calendar.FRIDAY)
    return nominal_date + timedelta(days=7 - weekday)


def get_occurrences_in_range(
    start: date, frequency: str, end_date: Optional[date],
    range_start: date, range_end: date,
    intended_day: Optional[int] = None,
    weekend_adjustment: str = "none",
) -> list[date]:
    """Compute effective occurrence dates within ``[range_start, range_end)``.

    Schedule advancement and end-date checks use nominal dates. The two-day
    scan margin captures nominal weekend occurrences that move across a range
    boundary; filtering happens only after calculating each effective date.
    """
    day = intended_day if intended_day else start.day
    nominal_range_start = range_start - timedelta(days=2)
    nominal_range_end = range_end + timedelta(days=2)
    occurrences: list[date] = []
    current = start
    while current < nominal_range_start:
        if end_date and current > end_date:
            return occurrences
        current = _advance_date(current, frequency, intended_day=day)
    while current < nominal_range_end:
        if end_date and current > end_date:
            break
        effective_date = adjust_weekend_date(current, weekend_adjustment)
        if range_start <= effective_date < range_end:
            occurrences.append(effective_date)
        current = _advance_date(current, frequency, intended_day=day)
        if len(occurrences) > 200:
            break
    return occurrences


async def generate_pending(
    session: AsyncSession, user_id: uuid.UUID, up_to: Optional[date] = None
) -> int:
    """Generate transactions for all pending recurring transactions up to a given date.
    If up_to is None, defaults to today. This allows the dashboard to pre-generate
    transactions for future months when the user navigates ahead.
    Returns the count of transactions generated."""
    # A person's recurring rows may live in several workspaces, and each
    # workspace keeps its own calendar, so "today" is resolved per workspace.
    # The query is bounded by the latest of those days and the loop below
    # applies each row's own cutoff.
    cutoffs: dict[uuid.UUID, date] = {}
    if up_to is None:
        workspace_ids = (
            await session.execute(
                select(RecurringTransaction.workspace_id)
                .where(
                    RecurringTransaction.user_id == user_id,
                    RecurringTransaction.is_active == True,
                    RecurringTransaction.auto_generate == True,
                )
                .distinct()
            )
        ).scalars().all()
        for ws_id in workspace_ids:
            cutoffs[ws_id] = today_in(await get_workspace_timezone(session, ws_id))
        if not cutoffs:
            return 0
        latest_cutoff = max(cutoffs.values())
    else:
        latest_cutoff = up_to

    result = await session.execute(
        select(RecurringTransaction)
        .where(
            RecurringTransaction.user_id == user_id,
            RecurringTransaction.is_active == True,
            RecurringTransaction.auto_generate == True,
            or_(
                and_(
                    RecurringTransaction.weekend_adjustment == "previous_friday",
                    RecurringTransaction.next_occurrence <= latest_cutoff + timedelta(days=2),
                ),
                and_(
                    RecurringTransaction.weekend_adjustment != "previous_friday",
                    RecurringTransaction.next_occurrence <= latest_cutoff,
                ),
            ),
        )
    )
    recurring_list = list(result.scalars().all())

    count = 0
    for recurring in recurring_list:
        # Legacy rows may exist with a null account_id from before account_id
        # was required. Skip them rather than crashing on Transaction's NOT NULL
        # constraint — the user should edit the recurring to fix it.
        if recurring.account_id is None:
            continue
        cutoff = up_to or cutoffs.get(recurring.workspace_id) or app_today()
        # Generate while the effective date is due. The nominal pointer remains
        # authoritative and is the only date used for schedule advancement and
        # end-date evaluation.
        while True:
            effective_occurrence = adjust_weekend_date(
                recurring.next_occurrence, recurring.weekend_adjustment
            )
            if effective_occurrence > cutoff:
                break
            if recurring.end_date and recurring.next_occurrence > recurring.end_date:
                recurring.is_active = False
                break

            # If a real transaction (synced/imported/manual) already covers this
            # occurrence, link it to the bill instead of writing a duplicate
            # placeholder (issue #116). Otherwise materialize the placeholder,
            # stamped with the recurring link so a later synced charge merges
            # into it rather than duplicating.
            existing_real = await recurring_match_service.find_real_tx_for_occurrence(
                session, recurring, effective_occurrence
            )
            if existing_real is not None:
                existing_real.recurring_transaction_id = recurring.id
            else:
                account = await session.get(Account, recurring.account_id)
                # Only occurrences that already came due reach this point, so
                # the row is a charge that happened rather than a forecast.
                # Whether to trust that depends on who else writes to the
                # account:
                #
                # Bank-synced: the incoming charge often fails to match this
                # placeholder, and two posted rows for one charge inflate the
                # balance silently. Holding the placeholder as pending keeps
                # it out of the actuals until something confirms it, so a
                # missed match costs a visible stale row instead of a wrong
                # number. Improving the match itself is the real fix (#588);
                # this is the guard until then.
                #
                # Manual: nothing else ever writes to the account, so there is
                # no charge to duplicate against and nothing that would ever
                # post the row. Pending there buys no safety and stops the
                # balance from moving.
                is_synced_account = account is not None and account.connection_id is not None
                transaction = Transaction(
                    user_id=user_id,
                    account_id=recurring.account_id,
                    category_id=recurring.category_id,
                    description=recurring.description,
                    amount=recurring.amount,
                    currency=recurring.currency,
                    date=effective_occurrence,
                    type=recurring.type,
                    source="recurring",
                    status="pending" if is_synced_account else "posted",
                    recurring_transaction_id=recurring.id,
                )
                apply_effective_date(transaction, account)
                session.add(transaction)
                await session.flush()
                await stamp_primary_amount(session, user_id, transaction)
                count += 1

            # Advance to next occurrence
            recurring.next_occurrence = _advance_date(
                recurring.next_occurrence, recurring.frequency,
                intended_day=recurring.day_of_month or recurring.start_date.day,
            )

            # Check again if past end_date after advancing
            if recurring.end_date and recurring.next_occurrence > recurring.end_date:
                recurring.is_active = False

    await session.commit()
    return count
