"""Credit-card statements and reference month (RFC toledo-features, Phase 4)

  - accounts.cycle_days_manual: statement_close_day / payment_due_day are
    now derived from the bills unless the user typed them (this flag).
  - accounts.forecast_offset_months / resolution_stats: the calibration of
    how open-bill purchases are placed (learned per account by recompute).
  - transactions.effective_date_source: why effective_date is what it is.
  - transactions.card_line_kind_reason: 'carried_over' credits.

Every account starts with derived days. An account with typed days and no
bills keeps them: there is nothing to derive from.

Revision ID: 100
Revises: 099
"""
from alembic import op
import sqlalchemy as sa

revision = "100"
down_revision = "099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("cycle_days_manual", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("accounts", sa.Column("forecast_offset_months", sa.SmallInteger(), nullable=True))
    op.add_column("accounts", sa.Column("resolution_stats", sa.JSON(), nullable=True))
    op.add_column("transactions", sa.Column("effective_date_source", sa.String(10), nullable=True))
    op.add_column("transactions", sa.Column("card_line_kind_reason", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("transactions", "card_line_kind_reason")
    op.drop_column("transactions", "effective_date_source")
    op.drop_column("accounts", "resolution_stats")
    op.drop_column("accounts", "forecast_offset_months")
    op.drop_column("accounts", "cycle_days_manual")
