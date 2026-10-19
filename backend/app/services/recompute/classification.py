"""Sticky classification (RFC toledo-features §5.3).

1. The highest-priority active rule in scope whose condition matches and
   that has a category action decides — unless the category is manual.
2. A manual choice is sticky. A category of unknown provenance (rows from
   before Rules v2: a category with no source) is treated the same way, so
   upgrading never re-categorizes history behind the user's back.
3. When the deciding rule stops matching the category stays, the rule id
   is cleared and the source stays 'rule' — another rule may replace it.
4. With no rule and no manual choice, the provider mapping applies and
   follows the mapping when it changes.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Classification:
    category_id: Optional[uuid.UUID]
    source: Optional[str]
    rule_id: Optional[uuid.UUID]


def is_sticky(current: Classification) -> bool:
    return current.source == "manual" or (current.source is None and current.category_id is not None)


def classify(
    current: Classification,
    decision,  # CategoryDecision | None
    provider_category_id: Optional[uuid.UUID],
) -> Classification:
    if is_sticky(current):
        return Classification(current.category_id, current.source, None)
    if decision is not None:
        return Classification(decision.category_id, "rule", decision.rule_id)
    if current.source == "rule":
        return Classification(current.category_id, "rule", None)
    if provider_category_id is not None:
        return Classification(provider_category_id, "provider", None)
    return Classification(None, None, None)
