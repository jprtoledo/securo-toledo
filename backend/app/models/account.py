import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Numeric, SmallInteger, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.bank_connection import BankConnection
    from app.models.institution import Institution
    from app.models.transaction import Transaction


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("bank_connections.id"), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Last 4 chars of the bank's identifier (IBAN, account or card number), when
    # the provider exposes one. Provider-owned like `name`: refreshed on sync,
    # not user-editable. Never holds the full identifier.
    masked_number: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    type: Mapped[str] = mapped_column(String(50))  # checking, savings, credit_card
    balance: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2), default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    balance_primary: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    credit_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    statement_close_day: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    payment_due_day: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    minimum_payment: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    card_brand: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    card_level: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    shared_balance_group: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # The institution this account is actually at. One connection can span
    # several (SimpleFIN — issue #345); null falls back to the connection's
    # own institution_name/logo_url. Eager (selectin) because serialization
    # always reads it and lazy loads raise in async sessions.
    institution_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("institutions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # Provider snapshot (RFC toledo-features §4.3; Pluggy creditData/bankData).
    available_credit_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    balance_close_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    balance_due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    overdraft_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    overdraft_used: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    card_holder_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # Max provider createdAt seen for the account; the next incremental sync
    # re-reads everything the provider ingested since (minus 24 h).
    provider_tx_created_watermark: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Credit cards (RFC toledo-features §6.3): statement_close_day and
    # payment_due_day are derived from the bills unless the user typed them,
    # which sets cycle_days_manual and stops the derivation.
    cycle_days_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Learned by recompute: month offset of Pluggy's bill forecast and the
    # measured accuracy/order of the open-bill fallbacks (§6.2).
    forecast_offset_months: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    resolution_stats: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    connection: Mapped[Optional["BankConnection"]] = relationship(back_populates="accounts")
    institution: Mapped[Optional["Institution"]] = relationship(
        back_populates="accounts", lazy="selectin"
    )
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="account", cascade="all, delete-orphan")
