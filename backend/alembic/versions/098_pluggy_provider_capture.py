"""Pluggy data capture and non-destructive sync (RFC toledo-features, Phase 2)

  - transactions: provider-owned columns promoted from raw_data
    (provider_id, operation_type, merchant, card, bill link as sent, ...),
    the user's sync lock, and the reasons behind automatic ignores
    (ignored_reason, provider_removed_at, duplicate_of_id,
    duplicate_override). Removed and duplicate rows reuse is_ignored, so
    every total already leaves them out.
  - accounts: creditData/bankData snapshot and the per-account watermark
    of the ingestion-time sweep.
  - credit_card_bills: closing date, installments flag, payments and the
    finance charges total.
  - bank_connections: provider-side item status, informational.

Every new column is nullable or has a default: nothing to backfill, the
next sync fills them.

Revision ID: 098
Revises: 097
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "098"
down_revision = "097"
branch_labels = None
depends_on = None

_TX_COLUMNS = (
    ("provider_id", sa.String(255)),
    ("provider_code", sa.String(255)),
    ("provider_created_at", sa.DateTime(timezone=True)),
    ("provider_updated_at", sa.DateTime(timezone=True)),
    ("provider_description_raw", sa.String(500)),
    ("operation_type", sa.String(50)),
    ("provider_category", sa.String(255)),
    ("provider_category_id", sa.String(50)),
    ("merchant_name", sa.String(255)),
    ("merchant_cnpj", sa.String(20)),
    ("merchant_mcc", sa.String(10)),
    ("card_number", sa.String(20)),
    ("payment_type", sa.String(20)),
    ("transaction_at", sa.DateTime(timezone=True)),
    ("provider_bill_external_id", sa.String(255)),
    ("bill_forecast_month", sa.Date()),
    ("bill_post_date", sa.Date()),
    ("amount_in_account_currency", sa.Numeric(precision=15, scale=2)),
    ("balance_after", sa.Numeric(precision=15, scale=2)),
    ("payment_data", sa.JSON()),
    ("ignored_reason", sa.String(20)),
    ("provider_removed_at", sa.DateTime(timezone=True)),
)

_ACCOUNT_COLUMNS = (
    ("available_credit_limit", sa.Numeric(precision=15, scale=2)),
    ("balance_close_date", sa.Date()),
    ("balance_due_date", sa.Date()),
    ("overdraft_limit", sa.Numeric(precision=15, scale=2)),
    ("overdraft_used", sa.Numeric(precision=15, scale=2)),
    ("card_holder_type", sa.String(30)),
    ("provider_tx_created_watermark", sa.DateTime(timezone=True)),
)

_BILL_COLUMNS = (
    ("closing_date", sa.Date()),
    ("allows_installments", sa.Boolean()),
    ("payments", sa.JSON()),
    ("finance_charges_total", sa.Numeric(precision=15, scale=2)),
)

_CONNECTION_COLUMNS = (
    ("provider_status", sa.String(50)),
    ("provider_execution_status", sa.String(80)),
    ("provider_last_updated_at", sa.DateTime(timezone=True)),
    ("consent_expires_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, type_ in _TX_COLUMNS:
        op.add_column("transactions", sa.Column(name, type_, nullable=True))
    op.add_column(
        "transactions",
        sa.Column("sync_locked", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "transactions",
        sa.Column("duplicate_of_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "transactions",
        sa.Column("duplicate_override", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_foreign_key(
        "transactions_duplicate_of_id_fkey",
        "transactions",
        "transactions",
        ["duplicate_of_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_transactions_duplicate_of_id", "transactions", ["duplicate_of_id"])
    # Re-keying by providerId (§4.5) looks rows up by account + provider id.
    op.create_index(
        "ix_transactions_account_provider_id",
        "transactions",
        ["account_id", "provider_id"],
        postgresql_where=sa.text("provider_id IS NOT NULL"),
    )

    for name, type_ in _ACCOUNT_COLUMNS:
        op.add_column("accounts", sa.Column(name, type_, nullable=True))
    for name, type_ in _BILL_COLUMNS:
        op.add_column("credit_card_bills", sa.Column(name, type_, nullable=True))
    for name, type_ in _CONNECTION_COLUMNS:
        op.add_column("bank_connections", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_CONNECTION_COLUMNS):
        op.drop_column("bank_connections", name)
    for name, _ in reversed(_BILL_COLUMNS):
        op.drop_column("credit_card_bills", name)
    for name, _ in reversed(_ACCOUNT_COLUMNS):
        op.drop_column("accounts", name)
    op.drop_index("ix_transactions_account_provider_id", table_name="transactions")
    op.drop_index("ix_transactions_duplicate_of_id", table_name="transactions")
    op.drop_constraint("transactions_duplicate_of_id_fkey", "transactions", type_="foreignkey")
    op.drop_column("transactions", "duplicate_override")
    op.drop_column("transactions", "duplicate_of_id")
    op.drop_column("transactions", "sync_locked")
    for name, _ in reversed(_TX_COLUMNS):
        op.drop_column("transactions", name)
