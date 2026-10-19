"""What a credit card's open and future statements will still receive
(RFC toledo-features §6.4): installment parcels the bank has not sent yet
(Phase 5) and expected recurring charges (Phase 6)."""
from __future__ import annotations

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.services.recompute.statements import Projection


async def card_projections(session: AsyncSession, account: Account, *, today: date) -> list[Projection]:
    from app.services.installment_plan_service import card_installment_projections
    from app.services.recurrence_service import card_recurring_projections

    return [
        *await card_installment_projections(session, account, today=today),
        *await card_recurring_projections(session, account, today=today),
    ]
