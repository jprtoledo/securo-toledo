"""The toledo-finance recurrence import script (RFC toledo-features §8.9, AC-6.16).

The end-to-end run (dry run, --apply, idempotent re-run, --with-decisions)
needs a toledo-finance Postgres; these tests pin the pure parts: the
condition conversion of Appendix B must produce conditions Rules v2 accepts
and evaluates as toledo did.
"""
import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.rule_engine import evaluate_node, rule_definition_errors

_spec = importlib.util.spec_from_file_location(
    "import_toledo_recurrences",
    Path(__file__).resolve().parent.parent / "scripts" / "import_toledo_recurrences.py",
)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)


def tx(**kw):
    base = dict(description="APPLE.COM/BILL", amount=Decimal("10.99"), type="debit", status="posted",
                merchant_name=None, operation_type=None, currency="BRL", installment_number=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_condition_conversion_is_valid_rules_v2():
    toledo = {"and": [
        {"field": "descriptionKey", "op": "startsWith", "value": "APPLECOMBILL"},
        {"field": "type", "op": "eq", "value": "DEBIT"},
        {"not": {"field": "merchantName", "op": "in", "value": ["99", "Cabify"]}},
        {"or": [{"field": "amount", "op": "gte", "value": 10}, {"field": "status", "op": "isNull"}]},
    ]}
    converted = script.convert_condition(toledo)
    assert converted["op"] == "and"
    assert converted["conditions"][1] == {"field": "type", "op": "equals", "value": "debit"}
    assert rule_definition_errors(None, "and", [converted], []) == []
    assert evaluate_node(converted, tx())
    assert not evaluate_node(converted, tx(type="credit"))
    assert not evaluate_node(converted, tx(merchant_name="Cabify"))


def test_leaf_condition_is_wrapped_in_a_group():
    converted = script.convert_condition('{"field": "description", "op": "contains", "value": "NETFLIX"}')
    assert converted == {"op": "and", "conditions": [{"field": "description", "op": "contains", "value": "NETFLIX"}]}


def test_card_only_fields_need_a_card_scope():
    converted = script.convert_condition({"field": "installmentNumber", "op": "gt", "value": 1})
    assert rule_definition_errors("credit_card", "and", [converted], []) == []


def test_unsupported_condition_is_reported():
    with pytest.raises(script.UnsupportedCondition):
        script.convert_condition({"field": "tags", "op": "eq", "value": "x"})


def test_next_occurrence():
    assert script.next_occurrence(date(2026, 1, 31), None, 1, 31, date(2026, 3, 15)) == date(2026, 3, 31)
    assert script.next_occurrence(date(2026, 1, 10), date(2026, 2, 10), 1, 10, date(2026, 6, 1)) == date(2026, 2, 10)
