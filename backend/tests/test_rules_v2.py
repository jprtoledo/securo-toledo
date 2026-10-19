"""RFC toledo-features Phase 3 — recompute engine and Rules v2 (AC-3.x)."""
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.rule import Rule
from app.models.transaction import Transaction
from app.schemas.rule import RuleAction, RuleCondition, RuleCreate, RuleExportPayload
from app.services import rule_service
from app.services.recompute.classification import Classification, classify
from app.services.recompute.rule_set import CategoryDecision
from app.services.recompute_service import recompute_account
from app.services.rule_engine import (
    description_key,
    evaluate_conditions,
    rule_definition_errors,
    strip_description,
)


def tx(**kw):
    base = dict(
        description="X", original_description=None, amount=Decimal("10"), type="debit",
        status="posted", currency="BRL", merchant_name=None, operation_type=None,
        installment_number=None, total_installments=None, payment_type=None,
        provider_category=None, card_number=None, provider_description_raw=None,
        payee=None, notes=None, date=date(2026, 10, 1), account_id=None, payee_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- pure: evaluator ----------------------------------------------------------


def test_ac_3_3_nested_and_negated_conditions():
    conditions = [
        {"field": "type", "op": "equals", "value": "debit"},
        {"not": {"op": "or", "conditions": [
            {"field": "description_key", "op": "starts_with", "value": "UBER"},
            {"field": "merchant_name", "op": "in", "value": ["99", "Cabify"]},
        ]}},
    ]
    assert evaluate_conditions("and", conditions, tx(description="IFOOD *RESTAURANTE"))
    assert not evaluate_conditions("and", conditions, tx(description="UBER *TRIP"))
    assert not evaluate_conditions("and", conditions, tx(description="RIDE", merchant_name="cabify"))
    assert not evaluate_conditions("and", conditions, tx(description="IFOOD", type="credit"))


def test_ac_3_4_description_key():
    assert description_key("APPLE.COM/BILL 123") == "APPLECOMBILL"
    assert description_key("Ação Café") == "ACAOCAFE"
    cond = [{"field": "description_key", "op": "starts_with", "value": "APPLECOMBILL"}]
    assert evaluate_conditions("and", cond, tx(description="APPLE.COM/BILL 123"))
    assert evaluate_conditions("and", cond, tx(description="Apple.com/bill"))
    assert not evaluate_conditions("and", cond, tx(description="APPLE STORE"))


@pytest.mark.parametrize(
    "condition,row,expected",
    [
        ({"field": "signed_amount", "op": "lt", "value": 0}, tx(type="debit"), True),
        ({"field": "signed_amount", "op": "gt", "value": 0}, tx(type="credit"), True),
        ({"field": "merchant_name", "op": "is_empty"}, tx(), True),
        ({"field": "merchant_name", "op": "is_not_empty"}, tx(merchant_name="X"), True),
        ({"field": "description", "op": "not_regex", "value": "AJUSTE|ESTORNO"}, tx(description="PGTO"), True),
        ({"field": "description", "op": "not_regex", "value": "AJUSTE"}, tx(description="ajuste x"), False),
        ({"field": "operation_type", "op": "equals", "value": "pagamento_fatura"},
         tx(operation_type="PAGAMENTO_FATURA"), True),
        ({"field": "installment_number", "op": "gte", "value": 2}, tx(installment_number=3), True),
        ({"field": "installment_number", "op": "lt", "value": 3}, tx(), False),  # missing never compares
        ({"field": "installment_number", "op": "not_equals", "value": 3}, tx(), True),
        ({"field": "currency", "op": "not_in", "value": ["USD", "EUR"]}, tx(), True),
        ({"field": "status", "op": "in", "value": ["pending"]}, tx(status="pending"), True),
        ({"field": "original_description", "op": "contains", "value": "bank"},
         tx(original_description="BANK TEXT"), True),
    ],
)
def test_new_fields_and_operators(condition, row, expected):
    assert evaluate_conditions("and", [condition], row) is expected


def test_account_kind_field():
    cond = [{"field": "account_kind", "op": "equals", "value": "credit_card"}]
    assert evaluate_conditions("and", cond, tx(), account_type="credit_card")
    assert not evaluate_conditions("and", cond, tx(), account_type="checking")


def test_strip_description():
    assert strip_description("SHEIN COM 02 DE 06", [r"\b\d{1,2}\s+DE\s+\d{1,2}\b"]) == "SHEIN COM"
    assert strip_description("PARC=103 PNEUSTORE", [r"\bPARC\s*=\s*\d{1,3}\s*"]) == "PNEUSTORE"


# --- pure: validator ----------------------------------------------------------


def _codes(errors):
    return [e["code"] for e in errors]


def test_ac_3_2_card_only_field_needs_card_scope():
    conditions = [{"field": "installment_number", "op": "gt", "value": 1}]
    actions = [{"op": "append_notes", "value": "#x"}]
    assert "field_not_in_scope" in _codes(rule_definition_errors(None, "and", conditions, actions))
    assert rule_definition_errors("credit_card", "and", conditions, actions) == []
    assert "field_not_in_scope" in _codes(rule_definition_errors("bank", "and", conditions, actions))


def test_card_actions_need_card_scope():
    actions = [{"op": "set_card_line_kind", "value": "payment"}]
    conditions = [{"field": "description", "op": "contains", "value": "PGTO"}]
    assert "action_not_in_scope" in _codes(rule_definition_errors(None, "and", conditions, actions))
    assert rule_definition_errors("credit_card", "and", conditions, actions) == []
    bad_kind = [{"op": "set_card_line_kind", "value": "refund"}]
    assert "invalid_card_kind" in _codes(rule_definition_errors("credit_card", "and", conditions, bad_kind))


def test_validator_matrix():
    acts = [{"op": "ignore"}]
    assert "unexpected_value" in _codes(rule_definition_errors(
        None, "and", [{"field": "merchant_name", "op": "is_empty", "value": "x"}], acts))
    assert "missing_value" in _codes(rule_definition_errors(
        None, "and", [{"field": "merchant_name", "op": "in", "value": []}], acts))
    assert "op_not_for_field" in _codes(rule_definition_errors(
        None, "and", [{"field": "operation_type", "op": "contains", "value": "PIX"}], acts))
    assert "invalid_regex" in _codes(rule_definition_errors(
        None, "and", [{"field": "merchant_name", "op": "regex", "value": "["}], acts))
    deep: dict = {"field": "description", "op": "contains", "value": "a"}
    for _ in range(5):
        deep = {"not": deep}
    assert "too_deep" in _codes(rule_definition_errors(None, "and", [deep], acts))
    # Legacy fields keep their legacy operators (AC-3.1).
    assert rule_definition_errors(None, "and", [{"field": "amount", "op": "contains", "value": "1"}], acts) == []


# --- pure: sticky classification state machine --------------------------------


CAT_A, CAT_B, CAT_P = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
RULE_1, RULE_2 = uuid.uuid4(), uuid.uuid4()


def test_classification_state_machine():
    empty = Classification(None, None, None)
    # provider mapping when nothing else
    s1 = classify(empty, None, CAT_P)
    assert s1 == Classification(CAT_P, "provider", None)
    # a rule overrides the provider
    s2 = classify(s1, CategoryDecision(CAT_A, RULE_1), CAT_P)
    assert s2 == Classification(CAT_A, "rule", RULE_1)
    # the user chooses: sticky against every rule
    manual = Classification(CAT_B, "manual", None)
    assert classify(manual, CategoryDecision(CAT_A, RULE_1), CAT_P) == manual
    # the rule stops matching: value stays, rule id cleared, still 'rule'
    s3 = classify(s2, None, CAT_P)
    assert s3 == Classification(CAT_A, "rule", None)
    # another rule can replace it later
    assert classify(s3, CategoryDecision(CAT_B, RULE_2), None) == Classification(CAT_B, "rule", RULE_2)
    # clear_category by rule
    assert classify(s2, CategoryDecision(None, RULE_2), None) == Classification(None, "rule", RULE_2)
    # the provider mapping follows when it changes
    assert classify(s1, None, CAT_B) == Classification(CAT_B, "provider", None)
    # legacy rows (category without provenance) are never overridden
    legacy = Classification(CAT_B, None, None)
    assert classify(legacy, CategoryDecision(CAT_A, RULE_1), CAT_P) == legacy


# --- service fixtures ---------------------------------------------------------


@pytest.fixture
async def card(session: AsyncSession, test_user, test_workspace) -> Account:
    acc = Account(user_id=test_user.id, workspace_id=test_workspace.id, name="Card",
                  type="credit_card", balance=Decimal("0"), currency="BRL")
    session.add(acc)
    await session.commit()
    return acc


@pytest.fixture
async def checking(session: AsyncSession, test_user, test_workspace) -> Account:
    acc = Account(user_id=test_user.id, workspace_id=test_workspace.id, name="Checking",
                  type="checking", balance=Decimal("0"), currency="BRL")
    session.add(acc)
    await session.commit()
    return acc


async def add_tx(session, account, description, amount="10", type_="debit", **kw) -> Transaction:
    row = Transaction(
        user_id=account.user_id, workspace_id=account.workspace_id, account_id=account.id,
        description=description, original_description=description, amount=Decimal(amount),
        currency="BRL", date=kw.pop("date", date(2026, 10, 5)), type=type_, source="sync", **kw,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def make_rule(session, workspace, user, name, conditions, actions, **kw) -> Rule:
    return await rule_service.create_rule(
        session, workspace.id, user.id,
        RuleCreate(name=name, conditions=conditions, actions=actions, **kw),
    )


# --- AC-3.1 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_3_1_old_rules_keep_working(session, test_user, test_workspace, test_categories, checking):
    # Saved before Rules v2: one group, legacy fields only, stored as raw JSON.
    rule = Rule(
        user_id=test_user.id, workspace_id=test_workspace.id, name="Legacy", conditions_op="and",
        conditions=[
            {"field": "type", "op": "equals", "value": "debit"},
            {"op": "or", "conditions": [
                {"field": "description", "op": "contains", "value": "UBER"},
                {"field": "description", "op": "starts_with", "value": "99"},
            ]},
        ],
        actions=[{"op": "set_category", "value": str(test_categories[1].id)}],
        priority=0, is_active=True,
    )
    session.add(rule)
    await session.commit()
    hit = await add_tx(session, checking, "UBER *TRIP")
    hit2 = await add_tx(session, checking, "99 POP")
    miss = await add_tx(session, checking, "IFOOD")
    miss_credit = await add_tx(session, checking, "UBER REFUND", type_="credit")
    await recompute_account(session, checking.id)
    for row, expected in ((hit, test_categories[1].id), (hit2, test_categories[1].id),
                          (miss, None), (miss_credit, None)):
        await session.refresh(row)
        assert row.category_id == expected
    errors = await rule_service.rule_errors(session, test_workspace.id, [rule])
    assert errors[rule.id] == []


# --- AC-3.2 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_3_2_scope(client: AsyncClient, auth_headers, session, test_user, test_workspace,
                            test_categories, card, checking):
    bad = await client.post("/api/rules", json={
        "name": "Parcelas", "conditions": [{"field": "installment_number", "op": "gt", "value": 1}],
        "actions": [{"op": "append_notes", "value": "#parcelado"}],
    }, headers=auth_headers)
    assert bad.status_code == 400

    resp = await client.post("/api/rules", json={
        "name": "Parcelas", "scope_account_kind": "credit_card",
        "conditions": [{"field": "installment_number", "op": "gt", "value": 1}],
        "actions": [{"op": "set_category", "value": str(test_categories[0].id)}],
    }, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    on_card = await add_tx(session, card, "LOJA", installment_number=2, total_installments=3)
    on_bank = await add_tx(session, checking, "LOJA", installment_number=2, total_installments=3)
    await recompute_account(session, card.id)
    await recompute_account(session, checking.id)
    await session.refresh(on_card)
    await session.refresh(on_bank)
    assert on_card.category_id == test_categories[0].id
    assert on_bank.category_id is None


@pytest.mark.asyncio
async def test_scope_account_id_forces_kind(session, test_user, test_workspace, test_categories, card):
    rule = await make_rule(
        session, test_workspace, test_user, "One card",
        [RuleCondition(field="card_number", op="equals", value="1234")],
        [RuleAction(op="set_category", value=str(test_categories[0].id))],
        scope_account_id=card.id,
    )
    assert rule.scope_account_kind == "credit_card"


# --- AC-3.5 / AC-3.6 / AC-3.7 ---------------------------------------------------


@pytest.mark.asyncio
async def test_ac_3_5_to_3_7_sticky_classification(
    client: AsyncClient, auth_headers, session, test_user, test_workspace, test_categories, checking
):
    transport, work = test_categories[1], test_categories[0]
    created = await client.post("/api/rules", json={
        "name": "Uber", "conditions": [{"field": "description", "op": "contains", "value": "UBER"}],
        "actions": [{"op": "set_category", "value": str(transport.id)}],
    }, headers=auth_headers)
    assert created.status_code == 201
    rule_id = uuid.UUID(created.json()["id"])

    a = await add_tx(session, checking, "UBER *TRIP 1")
    b = await add_tx(session, checking, "UBER *TRIP 2")
    await recompute_account(session, checking.id)
    # the user picks "Work" on one of them
    resp = await client.patch(f"/api/transactions/{a.id}", json={"category_id": str(work.id)}, headers=auth_headers)
    assert resp.status_code == 200
    await recompute_account(session, checking.id)
    await session.refresh(a)
    await session.refresh(b)
    assert (a.category_id, a.category_source) == (work.id, "manual")  # AC-3.5
    assert (b.category_id, b.category_source, b.category_rule_id) == (transport.id, "rule", rule_id)

    # AC-3.6: apply to existing with "Also override manual choices"
    resp = await client.patch(f"/api/rules/{rule_id}", json={
        "apply_to_existing": True, "overwrite_existing_categories": True,
    }, headers=auth_headers)
    assert resp.status_code == 200
    await session.refresh(a)
    assert (a.category_id, a.category_source) == (transport.id, "rule")

    # AC-3.7: the rule is disabled — the value stays, the rule id goes
    resp = await client.patch(f"/api/rules/{rule_id}", json={"is_active": False}, headers=auth_headers)
    assert resp.status_code == 200
    await session.refresh(b)
    assert (b.category_id, b.category_source, b.category_rule_id) == (transport.id, "rule", None)

    # ... and a later matching rule may replace it
    other = await client.post("/api/rules", json={
        "name": "Trips at work", "conditions": [{"field": "description", "op": "ends_with", "value": "2"}],
        "actions": [{"op": "set_category", "value": str(work.id)}],
    }, headers=auth_headers)
    assert other.status_code == 201
    await session.refresh(b)
    assert (b.category_id, b.category_source) == (work.id, "rule")


@pytest.mark.asyncio
async def test_clearing_a_manual_category_hands_the_row_back_to_rules(
    client: AsyncClient, auth_headers, session, test_categories, checking
):
    await client.post("/api/rules", json={
        "name": "Uber", "conditions": [{"field": "description", "op": "contains", "value": "UBER"}],
        "actions": [{"op": "set_category", "value": str(test_categories[1].id)}],
    }, headers=auth_headers)
    a = await add_tx(session, checking, "UBER", category_id=test_categories[0].id, category_source="manual")
    await recompute_account(session, checking.id)
    await session.refresh(a)
    assert a.category_id == test_categories[0].id
    await client.patch(f"/api/transactions/{a.id}", json={"category_id": None}, headers=auth_headers)
    await recompute_account(session, checking.id)
    await session.refresh(a)
    assert (a.category_id, a.category_source) == (test_categories[1].id, "rule")


# --- AC-3.8 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_3_8_rule_edits_are_retroactive_over_old_and_new_scope(
    client: AsyncClient, auth_headers, session, test_categories, card, checking
):
    resp = await client.post("/api/rules", json={
        "name": "Shop", "conditions": [{"field": "description", "op": "contains", "value": "LOJA"}],
        "actions": [{"op": "set_category", "value": str(test_categories[0].id)}],
    }, headers=auth_headers)
    rule_id = resp.json()["id"]
    on_bank = await add_tx(session, checking, "LOJA A")
    on_card = await add_tx(session, card, "LOJA B")
    manual = await add_tx(session, card, "LOJA C", category_id=test_categories[2].id, category_source="manual")
    await recompute_account(session, checking.id)
    await recompute_account(session, card.id)

    # narrow to cards and change the condition: both scopes are recomputed
    resp = await client.patch(f"/api/rules/{rule_id}", json={
        "scope_account_kind": "credit_card",
        "conditions": [{"field": "description", "op": "contains", "value": "LOJA B"}],
        "actions": [{"op": "set_category", "value": str(test_categories[1].id)}],
    }, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    for row in (on_bank, on_card, manual):
        await session.refresh(row)
    assert (on_bank.category_id, on_bank.category_rule_id) == (test_categories[0].id, None)
    assert on_card.category_id == test_categories[1].id
    assert manual.category_id == test_categories[2].id


# --- AC-3.9 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_3_9_provider_mapping_by_id(session, test_user, test_workspace, checking):
    groceries = Category(user_id=test_user.id, workspace_id=test_workspace.id, name="Mercado",
                         icon="x", color="#000000")
    session.add(groceries)
    await session.commit()
    row = await add_tx(session, checking, "SUPER X", provider_category_id="10000000",
                       provider_category="Something new")
    await recompute_account(session, checking.id)
    await session.refresh(row)
    assert (row.category_id, row.category_source) == (groceries.id, "provider")


# --- AC-3.10 / AC-3.11 / AC-3.12 -------------------------------------------------


@pytest.mark.asyncio
async def test_ac_3_10_card_line_kind_by_builtin_rule(session, test_user, test_workspace, card, checking):
    await rule_service.install_builtin_pack(session, test_workspace.id, test_user.id, "BR_CARDS")
    prev = await add_tx(session, card, "TOTAL DA FATURA ANTERIOR", "500")
    pay = await add_tx(session, card, "PGTO.BOLETO", "500", type_="credit")
    refund = await add_tx(session, card, "ESTORNO PAGAMENTO X", "40", type_="credit")
    purchase = await add_tx(session, card, "LOJA", "30")
    echo = await add_tx(session, card, "Pagamento recebido", "500", type_="credit",
                        operation_type="PAGAMENTO_FATURA")
    bank_row = await add_tx(session, checking, "TOTAL DA FATURA ANTERIOR")
    await recompute_account(session, card.id)
    await recompute_account(session, checking.id)
    rule = (await session.execute(select(Rule).where(Rule.builtin_key == "br.previous_balance"))).scalar_one()
    for row in (prev, pay, refund, purchase, echo, bank_row):
        await session.refresh(row)
    assert (prev.card_line_kind, prev.card_line_kind_source, prev.card_line_kind_rule_id) == (
        "previous_balance", "rule", rule.id)
    assert (pay.card_line_kind, pay.card_line_kind_source) == ("payment", "rule")
    assert (refund.card_line_kind, refund.card_line_kind_source) == ("credit", "default")
    assert (purchase.card_line_kind, purchase.card_line_kind_source) == ("purchase", "default")
    assert echo.card_line_kind == "payment"
    assert bank_row.card_line_kind is None


@pytest.mark.asyncio
async def test_ac_3_11_strip_marker_keeps_description(session, test_user, test_workspace, card):
    await rule_service.install_builtin_pack(session, test_workspace.id, test_user.id, "BR_CARDS")
    row = await add_tx(session, card, "SHEIN COM 02 DE 06")
    await recompute_account(session, card.id)
    await session.refresh(row)
    assert row.description == "SHEIN COM 02 DE 06"
    from app.services.recompute_service import load_rule_set

    rule_set = await load_rule_set(session, card)
    assert strip_description(row.description, rule_set.strip_patterns(row)) == "SHEIN COM"


@pytest.mark.asyncio
async def test_ac_3_12_restore_defaults(client: AsyncClient, auth_headers, session, test_workspace, test_user):
    await rule_service.install_builtin_pack(session, test_workspace.id, test_user.id, "BR_CARDS")
    rules = {r.builtin_key: r for r in (await session.execute(
        select(Rule).where(Rule.builtin_key.is_not(None)))).scalars().all()}
    edited = await client.patch(f"/api/rules/{rules['br.payment_by_text'].id}", json={
        "conditions": [{"field": "description", "op": "contains", "value": "PAGO"}],
    }, headers=auth_headers)
    assert edited.status_code == 200, edited.text
    assert (await client.delete(f"/api/rules/{rules['br.marker_slash'].id}", headers=auth_headers)).status_code == 204

    packs = (await client.get("/api/rules/packs", headers=auth_headers)).json()
    assert next(p for p in packs if p["code"] == "BR_CARDS")["installed"] is False

    resp = await client.post("/api/rules/packs/BR_CARDS/restore", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["created"] == 1
    listed = {r["builtin_key"]: r for r in (await client.get("/api/rules", headers=auth_headers)).json()
              if r["builtin_key"]}
    assert set(listed) == {"br.previous_balance", "br.payment_by_text", "br.marker_parc_eq",
                           "br.marker_parc", "br.marker_slash", "br.marker_de"}
    assert listed["br.payment_by_text"]["conditions"][1]["value"] == r"\b(PGTO|PAGTO|PAGAMENTO)\b"
    assert all(r["is_builtin"] and r["errors"] == [] for r in listed.values())


@pytest.mark.asyncio
async def test_builtin_pack_installed_for_new_workspaces(session, test_user, test_workspace):
    await rule_service.create_default_rules(session, test_user.id, "en", workspace_id=test_workspace.id)
    keys = {r.builtin_key for r in (await session.execute(
        select(Rule).where(Rule.workspace_id == test_workspace.id))).scalars().all()}
    assert "br.previous_balance" in keys


# --- AC-3.13 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_3_13_invalid_rules_are_visible_and_skipped(
    client: AsyncClient, auth_headers, session, test_user, test_workspace, test_categories, checking
):
    doomed = Category(user_id=test_user.id, workspace_id=test_workspace.id, name="Doomed", icon="x", color="#000000")
    session.add(doomed)
    await session.commit()
    rule = Rule(user_id=test_user.id, workspace_id=test_workspace.id, name="Stale", conditions_op="and",
                conditions=[{"field": "description", "op": "contains", "value": "X"}],
                actions=[{"op": "set_category", "value": str(uuid.uuid4())}], priority=0, is_active=True)
    broken = Rule(user_id=test_user.id, workspace_id=test_workspace.id, name="Broken", conditions_op="and",
                  conditions=[{"field": "description", "op": "regex", "value": "["}],
                  actions=[{"op": "ignore"}], priority=0, is_active=True)
    session.add_all([rule, broken])
    await session.commit()
    listed = {r["name"]: r for r in (await client.get("/api/rules", headers=auth_headers)).json()}
    assert [e["code"] for e in listed["Stale"]["errors"]] == ["category_missing"]
    assert [e["code"] for e in listed["Broken"]["errors"]] == ["invalid_regex"]
    row = await add_tx(session, checking, "X")
    stats = await recompute_account(session, checking.id)  # does not raise
    await session.refresh(row)
    assert row.category_id is None
    assert stats.rows == 1


# --- AC-3.14 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_3_14_recompute_is_idempotent(session, test_user, test_workspace, test_categories, card):
    await rule_service.install_builtin_pack(session, test_workspace.id, test_user.id, "BR_CARDS")
    await make_rule(session, test_workspace, test_user, "Food",
                    [RuleCondition(field="description", op="contains", value="IFOOD")],
                    [RuleAction(op="set_category", value=str(test_categories[0].id))])
    for d in ("IFOOD", "TOTAL DA FATURA ANTERIOR", "LOJA 1/3"):
        await add_tx(session, card, d)
    first = await recompute_account(session, card.id)
    assert first.changed > 0
    second = await recompute_account(session, card.id)
    assert second.changed == 0


# --- export / import ----------------------------------------------------------


@pytest.mark.asyncio
async def test_export_import_v2_and_v1(session, test_user, test_workspace, test_categories):
    await make_rule(session, test_workspace, test_user, "Card kind",
                    [{"not": {"field": "description", "op": "contains", "value": "AJUSTE"}}],
                    [RuleAction(op="set_card_line_kind", value="payment")],
                    scope_account_kind="credit_card")
    payload = await rule_service.export_rules(session, test_workspace.id)
    assert payload.version == 2
    item = payload.rules[0].model_dump()
    assert item["scope_account_kind"] == "credit_card"
    assert item["conditions"] == [{"not": {"field": "description", "op": "contains", "value": "AJUSTE"}}]

    result = await rule_service.import_rules(session, test_workspace.id, test_user.id, payload, overwrite=True)
    assert result.imported == 1
    v1 = RuleExportPayload.model_validate({
        "format": "securo-categorization-rules", "version": 1,
        "rules": [{"name": "Old", "conditions_op": "and",
                   "conditions": [{"field": "description", "op": "contains", "value": "UBER"}],
                   "actions": [{"op": "set_category", "value": test_categories[1].name}]}],
    })
    result = await rule_service.import_rules(session, test_workspace.id, test_user.id, v1, overwrite=True)
    assert result.imported == 1
