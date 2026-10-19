import uuid
from datetime import date as _Date
from decimal import Decimal
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

WeekendAdjustment = Literal["none", "previous_friday", "next_monday"]
RecurringFrequency = Literal[
    "weekly", "biweekly", "monthly", "quarterly", "semiannual", "yearly", "weekly_billed_monthly",
]
AmountMode = Literal["fixed", "variable"]
WeekCountBasis = Literal["previous_month", "same_month"]


class RecurrenceV2Fields(BaseModel):
    """Recurrences v2 (RFC toledo-features §8.2)."""

    # Condition in the rules language recognizing the charges (a node: leaf,
    # {op, conditions} group or {not: node}); null = the legacy matcher.
    match_condition: Optional[dict] = None
    amount_mode: Optional[AmountMode] = None
    amount_tolerance_pct: Optional[Decimal] = Field(default=None, ge=0, le=100)
    day_window: Optional[int] = Field(default=None, ge=0, le=31)
    billing_weekday: Optional[int] = Field(default=None, ge=0, le=6)
    week_count_basis: Optional[WeekCountBasis] = None
    # Other accounts that may pay a period (one whole charge each); the
    # forecast stays on account_id.
    extra_account_ids: Optional[list[uuid.UUID]] = None
DayOfMonth = Annotated[int, Field(ge=1, le=31)]


class RecurringTransactionCreate(RecurrenceV2Fields):
    description: str
    amount: Decimal
    currency: str = "USD"
    type: str  # debit, credit
    frequency: str  # weekly, biweekly, monthly, quarterly, semiannual, yearly
    weekend_adjustment: WeekendAdjustment = "none"
    day_of_month: Optional[DayOfMonth] = None
    start_date: _Date
    end_date: Optional[_Date] = None
    account_id: uuid.UUID
    category_id: Optional[uuid.UUID] = None
    skip_first: bool = False  # Set true when first occurrence already created as a transaction
    # Materialize occurrences as placeholders. Off by default since
    # Recurrences v2: forecasts are computed from the timeline.
    auto_generate: bool = False


class RecurringTransactionUpdate(RecurrenceV2Fields):
    description: Optional[str] = None
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    type: Optional[str] = None
    frequency: Optional[RecurringFrequency] = None
    weekend_adjustment: Optional[WeekendAdjustment] = None
    day_of_month: Optional[DayOfMonth] = None
    start_date: Optional[_Date] = None
    end_date: Optional[_Date] = None
    account_id: Optional[uuid.UUID] = None
    category_id: Optional[uuid.UUID] = None
    is_active: Optional[bool] = None
    auto_generate: Optional[bool] = None


class RecurringTransactionRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    account_id: Optional[uuid.UUID] = None
    category_id: Optional[uuid.UUID] = None
    description: str
    amount: Decimal
    currency: str
    type: str
    frequency: str
    weekend_adjustment: WeekendAdjustment = "none"
    day_of_month: Optional[int] = None
    start_date: _Date
    end_date: Optional[_Date] = None
    is_active: bool
    auto_generate: bool = True
    next_occurrence: _Date
    amount_primary: Optional[float] = None
    fx_rate_used: Optional[float] = None
    match_condition: Optional[dict] = None
    amount_mode: str = "fixed"
    amount_tolerance_pct: Decimal = Decimal("0")
    day_window: Optional[int] = None
    billing_weekday: Optional[int] = None
    week_count_basis: Optional[str] = None
    origin: str = "manual"
    import_ref: Optional[str] = None
    extra_account_ids: Optional[list[uuid.UUID]] = None
    # Timeline, alerts and the next expected slot (filled by the list endpoint).
    timeline: Optional[list["TimelineSlotRead"]] = None
    alerts: Optional[dict] = None
    next: Optional["TimelineSlotRead"] = None
    # Link stats of the recompute a change triggered.
    link_stats: Optional[dict] = None

    model_config = ConfigDict(from_attributes=True)


class TimelineSlotRead(BaseModel):
    period: _Date
    status: str  # posted | conflict | expected | late | missed | skipped
    expected_date: _Date
    month: _Date
    amount: Optional[Decimal] = None
    expected_amount: Decimal
    transaction_ids: list[uuid.UUID] = []
    conflict_ids: list[uuid.UUID] = []
    excluded: bool = False
    # The account that paid a posted/conflict slot.
    account_id: Optional[uuid.UUID] = None


class RecurringEndRequest(BaseModel):
    date: Optional[_Date] = None


class RecurringSkipRequest(BaseModel):
    period: _Date
    # False undoes the skip.
    skipped: bool = True


class RecurringLinkRequest(BaseModel):
    # A recurrence id, "none" ("not a recurring charge") or null (undo).
    recurring_id: Optional[str] = None


class RecurringReviewRequest(BaseModel):
    decision: Literal["accept", "reject"]


class SuggestionDismissRequest(BaseModel):
    key: str = Field(min_length=1, max_length=300)


RecurringTransactionRead.model_rebuild()
