import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger,
    String, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.category import Category
    from app.models.transaction import Transaction
    from app.models.user import User


class RecurringTransaction(Base):
    __tablename__ = "recurring_transactions"
    __table_args__ = (
        UniqueConstraint("user_id", "description", "frequency", "start_date", name="uq_recurring_tx"),
        CheckConstraint(
            "weekend_adjustment IN ('none', 'previous_friday', 'next_monday')",
            name="ck_recurring_transactions_weekend_adjustment",
        ),
        Index(
            "uq_recurring_transactions_workspace_import_ref",
            "workspace_id",
            "import_ref",
            unique=True,
            postgresql_where=text("import_ref IS NOT NULL"),
            sqlite_where=text("import_ref IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True)
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"), nullable=True)
    description: Mapped[str] = mapped_column(String(500))
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    type: Mapped[str] = mapped_column(String(10))  # debit, credit
    frequency: Mapped[str] = mapped_column(String(32))  # weekly, biweekly, monthly, quarterly, semiannual, yearly, weekly_billed_monthly
    weekend_adjustment: Mapped[str] = mapped_column(
        String(20), default="none", server_default="none"
    )
    day_of_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # When True (the default), generate_pending materializes this bill's due
    # occurrences into real transactions. When False, no placeholder is written
    # and the bill is only shown as a projection until the actual charge is
    # matched to it (e.g. from bank sync). Either way, incoming real
    # transactions are linked back to the bill to avoid duplicates.
    # True keeps the legacy path: placeholders are materialized and projected
    # from the next_occurrence pointer. Recurrences created through the API
    # default to false since Recurrences v2 (RFC toledo-features §8.2): their
    # forecast is computed from the timeline instead.
    auto_generate: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    next_occurrence: Mapped[date] = mapped_column(Date)
    amount_primary: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    fx_rate_used: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=20, scale=10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # --- Recurrences v2 (RFC toledo-features §8.2) ---
    # Condition in the rules language that recognizes the charges; null =
    # the legacy matcher (description similarity, exact amount).
    match_condition: Mapped[Optional[dict]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    # fixed: the expectation follows the last confirmed amount; variable:
    # the mean of the last 3 confirmed periods.
    amount_mode: Mapped[str] = mapped_column(String(10), default="fixed", server_default="fixed")
    amount_tolerance_pct: Mapped[Decimal] = mapped_column(
        Numeric(precision=5, scale=2), default=Decimal("0"), server_default="0"
    )
    # ± days around the expected date; null = default per frequency.
    day_window: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    # weekly_billed_monthly: weekday of the service (0 = Monday) and which
    # month's weekday count sets the charge ('previous_month' | 'same_month').
    billing_weekday: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    week_count_basis: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    origin: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")
    # Idempotency key of the toledo-finance import ("toledo:<uuid>").
    import_ref: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # Periods (ISO dates) the user marked as not happening: never forecast.
    skipped_periods: Mapped[Optional[list]] = mapped_column(JSON(none_as_null=True), nullable=True)
    # Other accounts (UUID strings) that may pay a period, one whole charge
    # each; the forecast stays on ``account_id``.
    extra_account_ids: Mapped[Optional[list]] = mapped_column(JSON(none_as_null=True), nullable=True)

    # Link stats of the recompute the last change triggered (not a column).
    link_stats = None

    user: Mapped["User"] = relationship()
    account: Mapped[Optional["Account"]] = relationship()
    category: Mapped[Optional["Category"]] = relationship()
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="recurring_transaction",
        foreign_keys="Transaction.recurring_transaction_id",
    )
