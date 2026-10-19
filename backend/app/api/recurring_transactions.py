import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import (
    WorkspaceContext,
    current_workspace,
    current_writable_workspace,
)
from app.models.account import Account
from app.schemas.recurring_transaction import (
    RecurringEndRequest,
    RecurringSkipRequest,
    RecurringTransactionCreate,
    RecurringTransactionRead,
    RecurringTransactionUpdate,
    TimelineSlotRead,
)
from app.services import recurrence_service, recurring_transaction_service

router = APIRouter(prefix="/api/recurring-transactions", tags=["recurring-transactions"])


def _slot(slot) -> TimelineSlotRead:
    return TimelineSlotRead(
        period=slot.period,
        status=slot.status,
        expected_date=slot.expected_date,
        month=slot.month,
        amount=slot.amount,
        expected_amount=slot.expected_amount,
        transaction_ids=slot.transaction_ids,
        conflict_ids=slot.conflict_ids,
        excluded=slot.excluded,
        account_id=slot.account_id,
    )


def _with_view(read: RecurringTransactionRead, view) -> RecurringTransactionRead:
    if view is None:
        return read
    read.timeline = [_slot(s) for s in view.timeline]
    read.alerts = view.alerts
    upcoming = recurrence_service.forecast(view.timeline)
    read.next = _slot(upcoming[0]) if upcoming else None
    return read


@router.get("", response_model=list[RecurringTransactionRead])
async def list_recurring_transactions(
    account_id: Optional[uuid.UUID] = Query(None),
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Every recurrence with its period timeline, alerts and next expected
    slot (RFC toledo-features §8.7)."""
    recs = await recurring_transaction_service.get_recurring_transactions(session, ctx.workspace.id)
    if account_id is not None:
        recs = [r for r in recs if r.account_id == account_id]
    views: dict = {}
    for acc_id in {r.account_id for r in recs if r.account_id}:
        account = await session.get(Account, acc_id)
        if account is not None:
            for view in await recurrence_service.views_for_account(session, account):
                views[view.recurrence.id] = view
    return [_with_view(RecurringTransactionRead.model_validate(r), views.get(r.id)) for r in recs]


@router.get("/{recurring_id}/timeline", response_model=RecurringTransactionRead)
async def get_recurring_timeline(
    recurring_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    recurring = await recurring_transaction_service.get_recurring_transaction(session, recurring_id, ctx.workspace.id)
    if not recurring or recurring.account_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recurring transaction not found")
    view = await recurrence_service.timeline_for(session, recurring)
    return _with_view(RecurringTransactionRead.model_validate(recurring), view)


@router.post("/{recurring_id}/end", response_model=RecurringTransactionRead)
async def end_recurring_transaction(
    recurring_id: uuid.UUID,
    data: RecurringEndRequest,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """"End recurrence": sets end_date (by default, the last missed period)."""
    recurring = await recurring_transaction_service.end_recurring_transaction(
        session, recurring_id, ctx.workspace.id, data.date
    )
    if not recurring:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recurring transaction not found")
    return recurring


@router.post("/{recurring_id}/skip", response_model=RecurringTransactionRead)
async def skip_recurring_period(
    recurring_id: uuid.UUID,
    data: RecurringSkipRequest,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Mark one occurrence as not happening (``skipped=false`` undoes it):
    it leaves the forecast and the reports."""
    try:
        recurring = await recurring_transaction_service.skip_recurring_period(
            session, recurring_id, ctx.workspace.id, data.period, data.skipped
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not recurring:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recurring transaction not found")
    view = await recurrence_service.timeline_for(session, recurring)
    return _with_view(RecurringTransactionRead.model_validate(recurring), view)


@router.post("", response_model=RecurringTransactionRead, status_code=status.HTTP_201_CREATED)
async def create_recurring_transaction(
    data: RecurringTransactionCreate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        return await recurring_transaction_service.create_recurring_transaction(
            session, ctx.workspace.id, ctx.user_id, data
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.patch("/{recurring_id}", response_model=RecurringTransactionRead)
async def update_recurring_transaction(
    recurring_id: uuid.UUID,
    data: RecurringTransactionUpdate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        recurring = await recurring_transaction_service.update_recurring_transaction(
            session, recurring_id, ctx.workspace.id, data
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not recurring:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recurring transaction not found")
    return recurring


@router.delete("/{recurring_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_recurring_transaction(
    recurring_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    deleted = await recurring_transaction_service.delete_recurring_transaction(
        session, recurring_id, ctx.workspace.id
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recurring transaction not found")


@router.post("/generate")
async def generate_recurring_transactions(
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    count = await recurring_transaction_service.generate_pending(session, ctx.user_id)
    return {"generated": count}
