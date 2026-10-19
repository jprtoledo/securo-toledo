import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import (
    WorkspaceContext,
    current_workspace,
    current_writable_workspace,
)
from app.schemas.account import (
    AccountCreate,
    AccountRead,
    AccountSummary,
    AccountUpdate,
    CreditCardBillRead,
)
from app.schemas.recurring_transaction import SuggestionDismissRequest
from app.services import account_service
from app.services.fx_rate_service import convert

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountRead])
async def list_accounts(
    include_closed: bool = False,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    accounts = await account_service.get_accounts(session, ctx.workspace.id, include_closed=include_closed)
    primary_currency = ctx.user.primary_currency
    for acc in accounts:
        if acc["currency"] != primary_currency:
            converted, _ = await convert(
                session, Decimal(str(acc["current_balance"])), acc["currency"], primary_currency,
            )
            acc["balance_primary"] = float(converted)
    return accounts


@router.get("/{account_id}/summary", response_model=AccountSummary)
async def get_account_summary(
    account_id: uuid.UUID,
    date_from: Optional[str] = Query(None, alias="from", description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, alias="to", description="YYYY-MM-DD"),
    bill_id: Optional[uuid.UUID] = Query(None, description="Aggregate by bill_id (issue #92); takes precedence over from/to"),
    unbilled_only: bool = Query(False, description="Cycle-math fallback only: exclude txs already linked to any bill"),
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    from_date = date.fromisoformat(date_from) if date_from else None
    to_date = date.fromisoformat(date_to) if date_to else None
    summary = await account_service.get_account_summary(
        session, account_id, ctx.workspace.id, date_from=from_date, date_to=to_date,
        bill_id=bill_id, unbilled_only=unbilled_only,
    )
    if not summary:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    account = await account_service.get_account(session, account_id, ctx.workspace.id)
    primary_currency = ctx.user.primary_currency
    if account and account.currency != primary_currency:
        bal, _ = await convert(session, Decimal(str(summary["current_balance"])), account.currency, primary_currency)
        opening, _ = await convert(session, Decimal(str(summary["opening_balance"])), account.currency, primary_currency)
        inc, _ = await convert(session, Decimal(str(summary["monthly_income"])), account.currency, primary_currency)
        exp, _ = await convert(session, Decimal(str(summary["monthly_expenses"])), account.currency, primary_currency)
        projected_inc, _ = await convert(session, Decimal(str(summary["projected_income"])), account.currency, primary_currency)
        projected_exp, _ = await convert(session, Decimal(str(summary["projected_expenses"])), account.currency, primary_currency)
        summary["current_balance_primary"] = float(bal)
        summary["opening_balance_primary"] = float(opening)
        summary["monthly_income_primary"] = float(inc)
        summary["monthly_expenses_primary"] = float(exp)
        summary["projected_income_primary"] = float(projected_inc)
        summary["projected_expenses_primary"] = float(projected_exp)

    return summary


@router.get("/{account_id}/balance-history")
async def get_account_balance_history(
    account_id: uuid.UUID,
    date_from: Optional[str] = Query(None, alias="from", description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, alias="to", description="YYYY-MM-DD"),
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    from_date = date.fromisoformat(date_from) if date_from else None
    to_date = date.fromisoformat(date_to) if date_to else None
    history = await account_service.get_account_balance_history(
        session, account_id, ctx.workspace.id, date_from=from_date, date_to=to_date,
    )
    if history is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    account = await account_service.get_account(session, account_id, ctx.workspace.id)
    primary_currency = ctx.user.primary_currency
    if account and account.currency != primary_currency:
        for point in history:
            point_date = date.fromisoformat(point["date"])
            converted, _ = await convert(
                session, Decimal(str(point["balance"])), account.currency, primary_currency, target_date=point_date,
            )
            point["balance_primary"] = float(converted)

    return history


@router.get("/{account_id}/bills", response_model=list[CreditCardBillRead])
async def get_account_bills(
    account_id: uuid.UUID,
    limit: int = Query(24, ge=1, le=200, description="Max bills to return, newest due_date first"),
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """List credit-card bills for an account, newest due_date first.

    Returns an empty list for non-CC accounts and for CC accounts that have
    no synced bills (provider doesn't expose them, or first sync hasn't
    happened). Issue #92.
    """
    bills = await account_service.get_credit_card_bills(
        session, account_id, ctx.workspace.id, limit=limit,
    )
    if bills is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return bills


@router.get("/{account_id}/statements", response_model=dict)
async def get_account_statements(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """One entry per bill month, from the first bill or row to the last
    future parcel (RFC toledo-features §6.4): state, status, totals,
    payments, reconciliation and projections."""
    from app.services.card_statement_service import get_statements

    account = await account_service.get_account(session, account_id, ctx.workspace.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if account.type != "credit_card":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a credit card")
    return await get_statements(session, account)


@router.get("/{account_id}/installment-plans", response_model=dict)
async def get_installment_plans(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """The card's installment plans with each parcel's timeline: posted,
    projected, missing (closed month the bank did not send) or before the
    history (RFC toledo-features §7.3)."""
    from app.core.app_clock import app_today
    from app.services.installment_plan_service import plan_view

    account = await account_service.get_account(session, account_id, ctx.workspace.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return await plan_view(session, account, today=app_today())


@router.get("/{account_id}/recurring-suggestions", response_model=list[dict])
async def get_recurring_suggestions(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Recurring charges the history suggests (RFC toledo-features §8.6),
    each a complete recurrence draft."""
    from dataclasses import asdict

    from app.services.recurrence_service import suggestions_for

    account = await account_service.get_account(session, account_id, ctx.workspace.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return [
        {**asdict(s), "account_id": account.id}
        for s in await suggestions_for(session, account)
    ]


@router.post("/{account_id}/recurring-suggestions/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_recurring_suggestion(
    account_id: uuid.UUID,
    data: SuggestionDismissRequest,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    from app.services.recurrence_service import dismiss_suggestion

    account = await account_service.get_account(session, account_id, ctx.workspace.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    await dismiss_suggestion(session, account, data.key)
    await session.commit()


@router.post("/{account_id}/recompute", response_model=dict)
async def recompute_account(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Re-derive the account's computed columns now and report what moved
    (RFC toledo-features §5.1; a debugging aid, recompute normally runs on
    its own after syncs and decisions)."""
    from app.services.recompute_service import recompute_account as run_recompute

    account = await account_service.get_account(session, account_id, ctx.workspace.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    stats = await run_recompute(session, account.id)
    await session.commit()
    return stats.as_dict()


@router.get("/{account_id}", response_model=AccountRead)
async def get_account(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    account = await account_service.get_account_with_balances(session, account_id, ctx.workspace.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return account


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
async def create_account(
    data: AccountCreate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    account = await account_service.create_account(session, ctx.workspace.id, ctx.user_id, data)
    return account_service.serialize_account(account, None, None)


@router.patch("/{account_id}", response_model=AccountRead)
async def update_account(
    account_id: uuid.UUID,
    data: AccountUpdate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        account = await account_service.update_account(session, account_id, ctx.workspace.id, data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    # Re-read post-commit so the response resolves the institution/display
    # name pair exactly like GET (update_account leaves the row expired).
    account = await account_service.get_account_with_balances(session, account_id, ctx.workspace.id)
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        deleted = await account_service.delete_account(session, account_id, ctx.workspace.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")


@router.post("/{account_id}/close", response_model=AccountRead)
async def close_account(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        account = await account_service.close_account(session, account_id, ctx.workspace.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return account


@router.post("/{account_id}/reopen", response_model=AccountRead)
async def reopen_account(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        account = await account_service.reopen_account(session, account_id, ctx.workspace.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return account
