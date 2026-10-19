import uuid
from datetime import date as _date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional, cast

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, JSON, Numeric, SmallInteger, String, event, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.core.database import Base


class _UUIDListJSON(TypeDecorator):
    """A list of UUIDs as JSON text — the SQLite stand-in for uuid[]."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return [str(v) for v in value] if value is not None else None

    def process_result_value(self, value, dialect):
        return [uuid.UUID(str(v)) for v in value] if value is not None else None

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.category import Category
    from app.models.credit_card_bill import CreditCardBill
    from app.models.import_log import ImportLog
    from app.models.payee import Payee
    from app.models.recurring_transaction import RecurringTransaction
    from app.models.transaction_attachment import TransactionAttachment
    from app.models.transaction_split import TransactionSplit


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # Provider's transaction ID
    description: Mapped[str] = mapped_column(String(500))
    original_description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    description_is_rule_managed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    date: Mapped[_date] = mapped_column(Date)
    # Effective date for cash-flow reporting. For regular accounts this equals
    # `date`. For credit card transactions it's the due date of the bill that
    # the transaction belongs to — so accrual-mode aggregations count the
    # purchase when it hits the user's cash, not when it was made.
    effective_date: Mapped[_date] = mapped_column(Date, index=True)
    type: Mapped[str] = mapped_column(String(10))  # debit, credit
    source: Mapped[str] = mapped_column(String(20))  # sync, ofx, csv, manual
    status: Mapped[str] = mapped_column(String(10), default="posted")  # posted, pending
    payee: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    payee_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("payees.id", ondelete="SET NULL"), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    raw_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    import_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("import_logs.id"), nullable=True)
    transfer_pair_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    # True on both legs of a cross-currency transfer whose destination amount
    # the user typed in, instead of letting it be converted at the market rate.
    # Both amounts are then observed facts, so editing one leg must not
    # re-derive the other from an FX rate.
    transfer_amount_explicit: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    amount_primary: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    fx_rate_used: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=20, scale=10), nullable=True)
    # Installment (parcelamento) metadata. Populated from provider data when available.
    # `installment_number` is 1-indexed (e.g. 3 for "3/12"). Storing alongside the
    # raw tx row so the door stays open to a plan view or manual entry later
    # without another migration.
    installment_number: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    total_installments: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    installment_total_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(precision=15, scale=2), nullable=True
    )
    installment_purchase_date: Mapped[Optional[_date]] = mapped_column(Date, nullable=True)
    # Stable identity for rows created by the manual installment-series
    # endpoint. Provider-synced installment rows leave this null because their
    # metadata is only a deduplication fingerprint, not a series identifier.
    installment_series_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    # User-set manual override for which bill cycle this tx belongs to. Null
    # by default; only meaningful for credit-card accounts. When set, beats
    # both Pluggy's billId and the cycle-math fallback (issue #92, the
    # LucasFidelis cycle_override request).
    effective_bill_date: Mapped[Optional[_date]] = mapped_column(Date, nullable=True)
    # FK to the bill this transaction belongs to (issue #92). Set during sync
    # for providers that expose a bills feed; null otherwise — in which case
    # the read path falls back to locally-computed cycle math via
    # apply_effective_date. ON DELETE SET NULL: if a bill row is removed
    # (re-sync, deletion), the transaction stays put and just unlinks.
    bill_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("credit_card_bills.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Ignore the row everywhere monetary totals are calculated, including the
    # account balance. The row remains stored and can still be shown in ledger
    # views unless the caller explicitly hides ignored transactions.
    is_ignored: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Keep the row in the ledger and current balance, but leave it out of P&L
    # reports. This differs from is_ignored, which also removes the row from
    # balance calculations. Balance adjustments use this distinction so the
    # reconciled balance stays exact without looking like income or spending.
    exclude_from_pnl: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # Link to the recurring bill this transaction fulfills (issue #116). Set when
    # a synced/imported/manual charge is matched to a recurring bill, or stamped
    # onto the placeholder generate_pending materializes. ON DELETE SET NULL: if
    # the recurring bill is removed, the transaction stays put and just unlinks.
    recurring_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("recurring_transactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # --- Provider capture (RFC toledo-features §4.3). Provider-owned: sync
    # rewrites them from the latest payload; the whole payload stays in
    # raw_data too. Only Pluggy fills them today.
    # Institution's own stable id (Open Finance). Survives Pluggy's
    # delete+recreate of a row, so it re-keys external_id (§4.5).
    provider_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider_code: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_description_raw: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    operation_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    provider_category: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    provider_category_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    merchant_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    merchant_cnpj: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    merchant_mcc: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    card_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # SINGLE / INSTALLMENT (credit card). SINGLE with an n/N counter is not
    # an installment purchase.
    payment_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Purchase timestamp to the second, when the bank sends one.
    transaction_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # The bill link as Pluggy sent it, even before that bill row is stored.
    provider_bill_external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # First day of the month Pluggy forecasts for an open-bill purchase.
    bill_forecast_month: Mapped[Optional[_date]] = mapped_column(Date, nullable=True)
    bill_post_date: Mapped[Optional[_date]] = mapped_column(Date, nullable=True)
    amount_in_account_currency: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(precision=15, scale=2), nullable=True
    )
    balance_after: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=15, scale=2), nullable=True)
    payment_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # "Don't update from the bank": only raw_data and provider_* columns are
    # refreshed while set; every visible field stays as the user left it.
    sync_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Why is_ignored is set: 'user' (or null on legacy rows), or one of the
    # automatic reasons 'provider_removed' / 'duplicate' that sync and
    # recompute set and clear on their own (§4.7).
    ignored_reason: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    provider_removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    duplicate_of_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The user said "not a duplicate": recompute never flags the row again.
    duplicate_override: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # --- Classification provenance (RFC §5.3). category_source: 'provider'
    # (provider category mapping), 'manual' (the user's choice, sticky),
    # 'rule' (category_rule_id while that rule still matches), or null.
    category_source: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    category_rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rules.id", ondelete="SET NULL"), nullable=True
    )
    # Card statement role of a credit-card row: purchase | credit | payment |
    # previous_balance. Derived by recompute (rule or default), never typed.
    card_line_kind: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    card_line_kind_source: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    card_line_kind_rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rules.id", ondelete="SET NULL"), nullable=True
    )
    # Why a kind was overridden by recompute itself: 'carried_over' (a credit
    # the bank repeats from the previous bill, counted as previous balance).
    card_line_kind_reason: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Why effective_date is what it is (§6.2): manual | bill | forecast |
    # post_date | cycle | tx_date. Null on rows recompute has not seen yet.
    effective_date_source: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # --- Installment plans (RFC §7.2). Derived by recompute: the plan, how
    # the parcel is linked (auto | conflict | manual) and why it conflicts.
    installment_plan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("installment_plans.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    installment_link: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # {"code": "month" | "amount", "expected": ...} — i18n key + params.
    installment_issue: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # The user's decision: this parcel belongs to that plan. Recompute
    # respects it and never re-evaluates the row.
    installment_plan_manual_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("installment_plans.id", ondelete="SET NULL"), nullable=True
    )
    # --- Recurrences v2 (RFC §8.2). Derived by recompute: the period the row
    # fulfills (nominal date), how it is linked (auto | conflict | manual),
    # why it conflicts, and whether it is the IOF line of a foreign charge.
    recurring_period: Mapped[Optional[_date]] = mapped_column(Date, nullable=True)
    recurring_link: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    recurring_issue: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    recurring_companion: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Decisions — sync never touches them (INV-2).
    recurring_manual_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recurring_transactions.id", ondelete="SET NULL"), nullable=True
    )
    recurring_manual_none: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    recurring_manual_excluded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    recurring_rejected_ids: Mapped[Optional[list]] = mapped_column(
        ARRAY(UUID(as_uuid=True)).with_variant(_UUIDListJSON(), "sqlite"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    account: Mapped["Account"] = relationship(back_populates="transactions")
    category: Mapped[Optional["Category"]] = relationship()
    bill: Mapped[Optional["CreditCardBill"]] = relationship()
    payee_entity: Mapped[Optional["Payee"]] = relationship(back_populates="transactions")
    recurring_transaction: Mapped[Optional["RecurringTransaction"]] = relationship(
        back_populates="transactions", foreign_keys=[recurring_transaction_id]
    )
    import_log: Mapped[Optional["ImportLog"]] = relationship(back_populates="transactions")
    attachments: Mapped[list["TransactionAttachment"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )
    splits: Mapped[list["TransactionSplit"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index(
            "ix_transactions_account_provider_id",
            "account_id",
            "provider_id",
            postgresql_where=text("provider_id IS NOT NULL"),
        ),
    )

    # Populated dynamically by the service (not DB columns).
    is_shared: bool = False
    viewer_share = cast(Optional[Decimal], None)
    group_id = cast(Optional[uuid.UUID], None)
    parent_owner_name = cast(Optional[str], None)
    attachment_count: int = 0
    payee_name = cast(Optional[str], None)


# Safety net: `effective_date` is NOT NULL. For non-CC transactions it always
# equals `date`, so if a call site (or test) forgets to set it, fall back
# silently. CC-aware service code still calls `apply_effective_date` explicitly
# so that cycle-based due dates are stored when the account has the metadata.
@event.listens_for(Transaction, "before_insert")
def _default_effective_date(mapper, connection, target):
    if target.effective_date is None:
        target.effective_date = target.date


@event.listens_for(Transaction, "before_update")
def _default_effective_date_on_update(mapper, connection, target):
    if target.effective_date is None:
        target.effective_date = target.date
