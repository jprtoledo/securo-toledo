"""Background recompute (RFC toledo-features §5.1).

Rule changes that reach many accounts recompute here instead of inside the
request that saved the rule.
"""
import asyncio
import logging
import uuid

from app.core.app_clock import use_timezone
from app.services.recompute_service import recompute_accounts
from app.tasks.sync_tasks import _make_session_maker
from app.worker import celery_app

logger = logging.getLogger(__name__)


async def _recompute(workspace_id: str, account_ids: list[str]) -> int:
    engine, session_maker = _make_session_maker()
    try:
        async with session_maker() as session:
            async with use_timezone(session, uuid.UUID(workspace_id), fresh=True):
                stats = await recompute_accounts(session, [uuid.UUID(a) for a in account_ids])
                await session.commit()
                return sum(s.changed for s in stats)
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.recompute_tasks.recompute_workspace")
def recompute_workspace(workspace_id: str, account_ids: list[str]) -> dict:
    changed = asyncio.run(_recompute(workspace_id, account_ids))
    logger.info("Recomputed %d account(s) of workspace %s: %d row(s) changed",
                len(account_ids), workspace_id, changed)
    return {"accounts": len(account_ids), "changed": changed}
