import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import WorkspaceContext, current_writable_workspace
from app.models.account import Account
from app.models.installment_plan import InstallmentPlan
from app.services import installment_plan_service
from app.services.recompute_service import recompute_account

router = APIRouter(prefix="/api/installment-plans", tags=["installment-plans"])


class MergeRequest(BaseModel):
    into_plan_id: uuid.UUID


@router.post("/{plan_id}/merge", response_model=dict)
async def merge_plan(
    plan_id: uuid.UUID,
    data: MergeRequest,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Merge plan A into plan B (same card, same number of installments, no
    overlapping numbers): every parcel of A is manually linked to B (AC-5.7)."""
    plan = await session.get(InstallmentPlan, plan_id)
    if plan is None or plan.workspace_id != ctx.workspace.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Installment plan not found")
    try:
        target = await installment_plan_service.merge_plans(session, plan, data.into_plan_id)
    except installment_plan_service.PlanDecisionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    account = await session.get(Account, plan.account_id)
    stats = await recompute_account(session, account.id)
    await session.commit()
    return {"plan_id": str(target.id), "recompute": stats.as_dict()}
