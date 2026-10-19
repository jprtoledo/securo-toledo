import uuid
from datetime import date as _date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InstallmentPlan(Base):
    """One purchase split in parcels (compra parcelada) — RFC toledo-features §7.

    ``detected`` plans come from the heuristic in recompute (stable id while
    their ``key`` is stable); ``manual`` plans exist because the user split
    or linked a parcel; ``series`` plans expose a manual installment series
    (transactions.installment_series_id) so the plan view is uniform.
    """

    __tablename__ = "installment_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(10))  # detected | manual | series
    # Heuristic grouping key (detected) or "series:<id>" (series); null for manual.
    key: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    total_installments: Mapped[int] = mapped_column(SmallInteger)
    installment_amount: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2))
    # Reference month of parcel 1 (first day of the month).
    first_month: Mapped[_date] = mapped_column(Date)
    purchase_date: Mapped[Optional[_date]] = mapped_column(Date, nullable=True)
    # The latest category the user chose on one of its parcels; new parcels get it.
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index(
            "uq_installment_plans_account_key",
            "account_id",
            "key",
            unique=True,
            postgresql_where=text("key IS NOT NULL"),
            sqlite_where=text("key IS NOT NULL"),
        ),
    )
