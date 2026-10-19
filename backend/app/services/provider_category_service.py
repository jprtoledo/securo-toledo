"""Provider category → workspace category (RFC toledo-features §5.3, rule 4).

By the provider's category id first (Pluggy catalog → default category key →
the workspace's category under any language variant of that default name),
then by the provider's category text through the original name map. Only
categories the workspace can still assign are returned.
"""
from __future__ import annotations

import uuid
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.providers.pluggy_categories import PLUGGY_CATEGORY_ID_MAP, PLUGGY_CATEGORY_MAP
from app.services.category_service import get_assignable_category_ids

Resolver = Callable[[object], Optional[uuid.UUID]]


def _never(_tx) -> Optional[uuid.UUID]:
    return None


async def load_resolver(
    session: AsyncSession, workspace_id: uuid.UUID, *, enabled: bool = True
) -> Resolver:
    """``enabled`` is the admin setting ``use_provider_categories``."""
    if not enabled:
        return _never
    from app.services.rule_service import _resolve_categories_by_internal_key

    assignable = await get_assignable_category_ids(session, workspace_id)
    rows = (
        await session.execute(
            select(Category.id, Category.name).where(Category.workspace_id == workspace_id)
        )
    ).all()
    by_name = {name: cid for cid, name in rows if cid in assignable}
    key_to_id = {
        key: uuid.UUID(str(cid))
        for key, cid in _resolve_categories_by_internal_key(
            {name: str(cid) for name, cid in by_name.items()}
        ).items()
    }

    def resolve(tx) -> Optional[uuid.UUID]:
        provider_id = getattr(tx, "provider_category_id", None)
        if provider_id:
            key = PLUGGY_CATEGORY_ID_MAP.get(str(provider_id))
            if key and key in key_to_id:
                return key_to_id[key]
        text = getattr(tx, "provider_category", None)
        if not text:
            return None
        name = PLUGGY_CATEGORY_MAP.get(text)
        if not name and " - " in text:
            name = PLUGGY_CATEGORY_MAP.get(text.split(" - ")[0])
        return by_name.get(name) if name else None

    return resolve
