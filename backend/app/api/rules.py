import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import (
    WorkspaceContext,
    current_workspace,
    current_writable_workspace,
)
from app.schemas.rule import (
    RuleError,
    RuleCreate,
    RuleCreateResponse,
    RuleImportRequest,
    RuleImportResponse,
    RuleMutationResponse,
    RulePreviewRequest,
    RulePreviewResponse,
    RuleRead,
    RuleUpdate,
)
from app.services import rule_service
from app.services.rule_service import DuplicateRuleError

router = APIRouter(prefix="/api/rules", tags=["rules"])


def _normalize_condition(condition: dict) -> dict:
    """Reduce one condition list entry to the keys that decide what it matches.

    A group entry carries its own operator and leaves instead of a field, so it
    has to be normalized recursively — flattening it to `field: None` would make
    every group compare equal and hide real edits from the change check below.
    """
    nested = condition.get("conditions")
    if isinstance(nested, list):
        return {
            "op": condition.get("op"),
            "conditions": [_normalize_condition(c) for c in nested],
        }
    return {
        "field": condition.get("field"),
        "op": condition.get("op"),
        "value": condition.get("value"),
    }


def _normalize_conditions(conditions: list[dict]) -> list[dict]:
    return [_normalize_condition(condition) for condition in conditions]


def _rule_match_definition_changed(rule: RuleRead, data: RuleUpdate) -> bool:
    update_data = data.model_dump(exclude_unset=True)
    if update_data.get("is_active") is True and not rule.is_active:
        return True
    if (
        "conditions_op" in update_data
        and update_data["conditions_op"] != rule.conditions_op
    ):
        return True
    if "conditions" in update_data:
        if _normalize_conditions(update_data["conditions"] or []) != _normalize_conditions(
            rule.conditions or []
        ):
            return True
    if "actions" in update_data:
        return [a.model_dump() for a in data.actions or []] != (rule.actions or [])
    return False


_MATCH_FIELDS = {
    "conditions_op", "conditions", "actions", "priority", "is_active",
    "scope_account_kind", "scope_account_id",
}


def _scope(rule) -> tuple:
    return (rule.scope_account_kind, rule.scope_account_id)


async def _read_with_errors(session, workspace_id, rules) -> list[RuleRead]:
    errors = await rule_service.rule_errors(session, workspace_id, rules)
    out = []
    for rule in rules:
        item = RuleRead.model_validate(rule)
        item.errors = [RuleError(**e) for e in errors.get(rule.id, [])]
        out.append(item)
    return out


@router.get("", response_model=list[RuleRead])
async def list_rules(
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Every rule with the problems that make the engine skip it (AC-3.13)."""
    rules = await rule_service.get_rules(session, ctx.workspace.id)
    return await _read_with_errors(session, ctx.workspace.id, rules)


@router.post("", response_model=RuleCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_rule(
    data: RuleCreate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        rule = await rule_service.create_rule(session, ctx.workspace.id, ctx.user_id, data)
    except DuplicateRuleError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A rule with this name already exists",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    # Apply the new rule to existing transactions so it takes effect on history
    # immediately, and report how many were touched for a transparent toast.
    # Its non-category actions only touch history when asked to; categories
    # and card line kinds are derived, so the accounts it reaches are
    # recomputed either way (RFC toledo-features §5.3, AC-3.8).
    applied_count = (
        await rule_service.apply_single_rule(
            session,
            ctx.workspace.id,
            rule,
            overwrite_existing_categories=data.overwrite_existing_categories,
        )
        if data.apply_to_existing
        else 0
    )
    applied_count += await rule_service.recompute_rule_scopes(
        session, ctx.workspace.id, [_scope(rule)]
    )
    await session.refresh(rule)
    response = RuleCreateResponse.model_validate(rule)
    response.applied_count = applied_count
    return response


@router.post("/preview", response_model=RulePreviewResponse)
async def preview_rule(
    data: RulePreviewRequest,
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Show which existing transactions a draft rule would match, and how it
    would change them, without saving anything."""
    try:
        return await rule_service.preview_rule(
            session,
            ctx.workspace.id,
            data.conditions_op,
            [c.model_dump() for c in data.conditions],
            [a.model_dump() for a in data.actions],
            is_active=data.is_active,
            apply_to_existing=data.apply_to_existing,
            overwrite_existing_categories=data.overwrite_existing_categories,
            limit=data.limit,
            offset=data.offset,
            scope_account_kind=data.scope_account_kind,
            scope_account_id=data.scope_account_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/export")
async def export_rules(
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    payload = await rule_service.export_rules(session, ctx.workspace.id)
    return JSONResponse(
        content=payload.model_dump(mode="json"),
        headers={
            "Content-Disposition": 'attachment; filename="securo-categorization-rules.json"',
        },
    )


@router.post("/import", response_model=RuleImportResponse)
async def import_rules(
    data: RuleImportRequest,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    try:
        return await rule_service.import_rules(
            session,
            ctx.workspace.id,
            ctx.user_id,
            data.payload,
            overwrite=data.overwrite,
        )
    except DuplicateRuleError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Import would overwrite existing rules. Confirm overwrite to continue.",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.patch("/{rule_id}", response_model=RuleMutationResponse)
async def update_rule(
    rule_id: uuid.UUID,
    data: RuleUpdate,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    current_rule = await rule_service.get_rule(session, rule_id, ctx.workspace.id)
    if not current_rule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    if data.apply_to_existing is None:
        should_apply = _rule_match_definition_changed(RuleRead.model_validate(current_rule), data)
    else:
        should_apply = data.apply_to_existing
    old_scope = _scope(current_rule)
    should_recompute = bool(_MATCH_FIELDS & data.model_dump(exclude_unset=True).keys())

    try:
        rule = await rule_service.update_rule(session, rule_id, ctx.workspace.id, data)
    except DuplicateRuleError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A rule with this name already exists",
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not rule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    applied_count = (
        await rule_service.apply_single_rule(
            session,
            ctx.workspace.id,
            rule,
            overwrite_existing_categories=data.overwrite_existing_categories,
        )
        if should_apply
        else 0
    )
    if should_recompute or should_apply:
        # The old and the new scope: rows the rule stopped reaching lose its
        # derived effects, rows it now reaches gain them (AC-3.8).
        applied_count += await rule_service.recompute_rule_scopes(
            session, ctx.workspace.id, [old_scope, _scope(rule)]
        )
        await session.refresh(rule)
    response = RuleMutationResponse.model_validate(rule)
    response.applied_count = applied_count
    return response


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    current_rule = await rule_service.get_rule(session, rule_id, ctx.workspace.id)
    old_scope = _scope(current_rule) if current_rule else None
    deleted = await rule_service.delete_rule(session, rule_id, ctx.workspace.id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    await rule_service.recompute_rule_scopes(session, ctx.workspace.id, [old_scope])


@router.get("/packs", response_model=list[dict])
async def list_rule_packs(
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """List available country-specific rule packs with installed status."""
    installed_map = await rule_service.get_installed_packs(
        session, ctx.workspace.id, ctx.user_id
    )
    packs = []
    for code, pack in rule_service.RULE_PACKS.items():
        packs.append({
            "code": code,
            "name": pack["name"],
            "flag": pack["flag"],
            "rule_count": len(pack["rules"]),
            "installed": installed_map.get(code, False),
        })
    lang = (ctx.user.preferences or {}).get("language", "en")
    for code, pack in rule_service.BUILTIN_PACKS.items():
        packs.append({
            "code": code,
            "name": rule_service.localized(pack["name"], lang),
            "flag": pack["flag"],
            "rule_count": len(pack["rules"]),
            "installed": await rule_service.builtin_pack_installed(session, ctx.workspace.id, code),
            "builtin": True,
        })
    return packs


@router.post("/packs/{pack_code}/install", response_model=dict)
async def install_rule_pack(
    pack_code: str,
    create_missing_categories: bool = False,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Install a country-specific rule pack.

    `create_missing_categories=true` opts into seeding any default
    categories the pack needs that the user doesn't already have.
    """
    lang = (ctx.user.preferences or {}).get("language", "pt-BR")
    if pack_code in rule_service.BUILTIN_PACKS:
        builtin = await rule_service.install_builtin_pack(
            session, ctx.workspace.id, ctx.user_id, pack_code, lang
        )
        await rule_service.recompute_rule_scopes(session, ctx.workspace.id, [("credit_card", None)])
        return {"installed": len(builtin.created), "unresolved": 0, "categories_created": 0}
    if pack_code not in rule_service.RULE_PACKS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule pack not found")
    result = await rule_service.install_rule_pack(
        session,
        ctx.workspace.id,
        ctx.user_id,
        pack_code,
        lang,
        create_missing_categories=create_missing_categories,
    )
    return {
        "installed": len(result.rules),
        "unresolved": result.unresolved,
        "categories_created": result.categories_created,
    }


@router.post("/packs/{pack_code}/restore", response_model=dict)
async def restore_builtin_pack(
    pack_code: str,
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """"Restore defaults" (AC-3.12): recreate deleted built-in rules and reset
    edited ones to their original definition."""
    if pack_code not in rule_service.BUILTIN_PACKS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule pack not found")
    lang = (ctx.user.preferences or {}).get("language", "pt-BR")
    result = await rule_service.install_builtin_pack(
        session, ctx.workspace.id, ctx.user_id, pack_code, lang, restore=True
    )
    await rule_service.recompute_rule_scopes(session, ctx.workspace.id, [("credit_card", None)])
    return {"created": len(result.created), "restored": len(result.restored)}


@router.post("/apply-all", response_model=dict)
async def apply_all_rules(
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    """Re-apply all active rules to all existing transactions."""
    count = await rule_service.apply_all_rules(session, ctx.workspace.id)
    return {"applied": count}
