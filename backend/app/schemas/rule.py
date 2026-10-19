# backend/app/schemas/rule.py
import datetime
import uuid
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_VALUELESS_OPS = {"is_empty", "is_not_empty"}
_LIST_OPS = {"in", "not_in"}


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


class RuleCondition(BaseModel):
    field: str   # see rule_engine.FIELD_TYPES
    op: str      # see rule_engine.allowed_ops
    value: Any = None  # str/number, a list for in/not_in, absent for is_empty/is_not_empty

    @model_validator(mode="after")
    def value_matches_operator(self) -> "RuleCondition":
        """Reject blank values — they silently match every transaction.

        A blank value turns `contains`/`starts_with`/`ends_with`/`regex` into a
        tautology, and numeric comparisons fall back to 0, so the rule applies
        its actions to the whole ledger. Explicit `0` and `False` stay valid.
        Value-less operators (is_empty / is_not_empty) must not carry one.
        """
        v = self.value
        if self.op in _VALUELESS_OPS:
            if not _blank(v) and v != []:
                raise ValueError(f"Operator '{self.op}' takes no value")
            self.value = None
            return self
        if self.op in _LIST_OPS:
            items = v if isinstance(v, list) else [v]
            if not items or any(_blank(x) for x in items):
                raise ValueError("Condition value cannot be blank")
            return self
        if _blank(v):
            raise ValueError("Condition value cannot be blank")
        return self


class RuleConditionGroup(BaseModel):
    """A nested group of conditions joined by its own operator.

    Groups let a rule mix AND and OR — `type is debit AND (contains UBER OR
    contains 99POP)` — and nest to any depth up to 5 (Rules v2; the engine
    enforces the limit), alongside negations.
    """

    op: str = "or"   # and, or
    conditions: list["RuleConditionNode"]

    @field_validator("op")
    @classmethod
    def op_must_be_and_or(cls, v: str) -> str:
        if v not in ("and", "or"):
            raise ValueError("Condition group operator must be 'and' or 'or'")
        return v

    @field_validator("conditions")
    @classmethod
    def group_must_not_be_empty(cls, v: list) -> list:
        """An empty group never matches, so it can only make a rule confusing."""
        if not v:
            raise ValueError("Condition group cannot be empty")
        return v


class RuleConditionNot(BaseModel):
    """`{"not": node}` — matches when the node does not."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    not_: "RuleConditionNode" = Field(alias="not")


# A condition node is a leaf, a group or a negation. The shapes are disjoint —
# a leaf has `field`, a group `conditions`, a negation `not` — so Pydantic's
# smart union resolves them without a discriminator.
RuleConditionNode = Union[RuleConditionGroup, RuleConditionNot, RuleCondition]
RuleConditionGroup.model_rebuild()
RuleConditionNot.model_rebuild()


def dump_conditions(conditions: list) -> list[dict]:
    """Stored JSON of a validated condition list (negations as `{"not": …}`)."""
    return [
        c.model_dump(by_alias=True) if hasattr(c, "model_dump") else c
        for c in conditions or []
    ]


class RuleAction(BaseModel):
    # set_category, clear_category, set_payee, set_description, append_notes,
    # ignore, set_card_line_kind (credit cards), strip_installment_marker
    # (credit cards)
    op: str
    value: Any = None   # entity UUID, text, card line kind or pattern; none for clear_category/ignore


ScopeKind = Optional[Literal["bank", "credit_card"]]


class RuleCreate(BaseModel):
    name: str
    conditions_op: str = "and"
    conditions: list[RuleConditionNode]
    actions: list[RuleAction]
    priority: int = 0
    is_active: bool = True
    scope_account_kind: ScopeKind = None
    scope_account_id: Optional[uuid.UUID] = None
    apply_to_existing: bool = True
    # "Also override manual choices": rules replace categories the user set by hand.
    overwrite_existing_categories: bool = False


class RuleUpdate(BaseModel):
    name: Optional[str] = None
    conditions_op: Optional[str] = None
    conditions: Optional[list[RuleConditionNode]] = None
    actions: Optional[list[RuleAction]] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    scope_account_kind: ScopeKind = None
    scope_account_id: Optional[uuid.UUID] = None
    apply_to_existing: Optional[bool] = None
    overwrite_existing_categories: bool = False


class RuleError(BaseModel):
    """A problem that makes the engine skip the rule (shown, not hidden)."""

    code: str
    message: str
    params: dict = {}


class RuleRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    conditions_op: str
    conditions: list[dict]
    actions: list[dict]
    priority: int
    is_active: bool
    scope_account_kind: Optional[str] = None
    scope_account_id: Optional[uuid.UUID] = None
    is_builtin: bool = False
    builtin_key: Optional[str] = None
    errors: list[RuleError] = []

    model_config = ConfigDict(from_attributes=True)


class RuleMutationResponse(RuleRead):
    """A changed rule plus how many existing transactions it just affected."""

    applied_count: int = 0


class RuleCreateResponse(RuleMutationResponse):
    """A created rule response kept for API compatibility."""


class RuleExportItem(BaseModel):
    name: str
    conditions_op: str = "and"
    conditions: list[RuleConditionNode]
    actions: list[RuleAction]
    priority: int = 0
    is_active: bool = True
    # v2 (version 1 files simply lack them).
    scope_account_kind: ScopeKind = None
    builtin_key: Optional[str] = None


class RuleExportPayload(BaseModel):
    format: str = "securo-categorization-rules"
    # 2 = Rules v2 (scope, nested/negated conditions, new fields/actions).
    # Version 1 files are still read.
    version: int = 2
    rules: list[RuleExportItem]


class RuleImportRequest(BaseModel):
    payload: RuleExportPayload
    overwrite: bool = False


class RuleImportResponse(BaseModel):
    imported: int
    skipped: int
    overwritten: int


class RulePreviewRequest(BaseModel):
    """A draft rule sent from the editor, before it is saved.

    Carries the same save-time flags as `RuleCreate` — the preview answers
    "what happens when I save this?", and saving an inactive rule, or one not
    being applied to existing transactions, changes nothing right now.
    Name and priority play no part: neither decides what a rule matches.
    """

    conditions_op: str = "and"
    conditions: list[RuleConditionNode]
    actions: list[RuleAction] = []
    is_active: bool = True
    scope_account_kind: ScopeKind = None
    scope_account_id: Optional[uuid.UUID] = None
    apply_to_existing: bool = True
    overwrite_existing_categories: bool = False
    limit: int = Field(default=20, ge=1, le=100)
    # The sample is a window over the matches, newest first. Counts are exact
    # whatever the window is, so the editor pages through a broad rule's
    # matches instead of judging it by the first screenful.
    offset: int = Field(default=0, ge=0)


class RulePreviewItem(BaseModel):
    """One matched transaction plus the category the draft rule would leave it in."""

    id: uuid.UUID
    date: datetime.date
    description: str
    # float, not Decimal: this is display data for the editor's preview table,
    # and Decimal would serialize as a JSON string the UI has to coerce back.
    amount: float
    currency: str
    type: str
    current_category_id: Optional[uuid.UUID] = None
    current_category_name: Optional[str] = None
    new_category_id: Optional[uuid.UUID] = None
    new_category_name: Optional[str] = None
    # False when the rule matches but leaves the transaction as it is — most
    # often because it already has a category and the draft does not overwrite.
    will_change: bool


class RulePreviewResponse(BaseModel):
    matched: int
    will_change: int
    # False when the draft's own flags mean saving it touches nothing now: an
    # inactive rule, or one not being applied to existing transactions. The
    # matches are still reported, so the conditions can be checked either way.
    will_apply: bool
    # The requested window of the matches — `offset` through `offset + limit`,
    # newest first. Compare `offset + len(sample)` with `matched` to know
    # whether more can be fetched.
    sample: list[RulePreviewItem]
    offset: int = 0
