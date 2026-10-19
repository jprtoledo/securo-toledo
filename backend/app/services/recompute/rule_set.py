"""The active rules that apply to one account, compiled once per recompute.

Pure: built from already-loaded Rule rows. Invalid rules (per
``rule_engine.rule_definition_errors``) are skipped, never fatal (AC-3.13).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.services import rule_engine


def rule_matches(rule, tx, account_type: Optional[str]) -> bool:
    """Match on the current row, then on its bank text: a row a rule renamed
    must still be recognized by rules written against the bank's wording."""
    if rule_engine.evaluate_conditions(rule.conditions_op, rule.conditions or [], tx, account_type):
        return True
    original = getattr(tx, "original_description", None)
    if original is not None and original != getattr(tx, "description", None):
        return rule_engine.evaluate_conditions(
            rule.conditions_op, rule.conditions or [], _WithDescription(tx, original), account_type
        )
    return False


class _WithDescription:
    """Read-only view of a row with another description (never an ORM copy:
    writing through one would dirty the real row)."""

    def __init__(self, tx, description: str) -> None:
        self._tx = tx
        self.description = description

    def __getattr__(self, name):
        return getattr(self._tx, name)


@dataclass
class CategoryDecision:
    category_id: Optional[uuid.UUID]
    rule_id: uuid.UUID


@dataclass
class RuleSet:
    rules: list
    account_type: Optional[str]
    assignable_category_ids: set = field(default_factory=set)

    @classmethod
    def compile(
        cls,
        rules: Iterable,
        *,
        account_id,
        account_type: Optional[str],
        assignable_category_ids: Iterable[uuid.UUID] = (),
    ) -> "RuleSet":
        active = [
            r for r in rules
            if r.is_active
            and rule_engine.rule_in_scope(r, account_id, account_type)
            and not rule_engine.rule_definition_errors(
                r.scope_account_kind, r.conditions_op, r.conditions or [], r.actions or []
            )
        ]
        active.sort(key=lambda r: (r.priority or 0, str(r.id)))
        return cls(active, account_type, set(assignable_category_ids))

    def category_decision(self, tx) -> Optional[CategoryDecision]:
        """Highest-priority matching rule with a category action. A rule
        pointing at a hidden or deleted category is skipped (as today)."""
        for rule in self.rules:
            action = next(
                (a for a in rule.actions or [] if a.get("op") in rule_engine.CATEGORY_ACTIONS),
                None,
            )
            if action is None:
                continue
            target: Optional[uuid.UUID] = None
            if action["op"] == "set_category":
                try:
                    target = uuid.UUID(str(action.get("value")))
                except (TypeError, ValueError):
                    continue
                if target not in self.assignable_category_ids:
                    continue
            if rule_matches(rule, tx, self.account_type):
                return CategoryDecision(target, rule.id)
        return None

    def card_kind(self, tx):
        """(kind, rule_id) of the highest-priority set_card_line_kind rule."""
        kind, rule = rule_engine.card_line_kind_for(self.rules, tx, self.account_type)
        return (kind, rule.id) if rule is not None else (None, None)

    def strip_patterns(self, tx) -> list[str]:
        return rule_engine.strip_patterns_for(self.rules, tx, self.account_type)
