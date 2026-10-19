"""Recurrences v2 (RFC toledo-features, Phase 6)

  - recurring_transactions: a match condition in the rules language,
    fixed/variable amounts with a tolerance, a day window, the
    weekly-billed-monthly frequency (billing weekday + count basis), the
    origin and the idempotency key of the toledo-finance import. Recurrences
    created through the API no longer auto-generate placeholders by default
    (forecasts are computed); existing rows keep their setting.
  - transactions: the derived link (period, link kind, issue, IOF
    companion) and the user's decisions (manual link, "not recurring",
    accepted-but-excluded, rejected recurrences).
  - recurring_suggestion_dismissals: suggestions dismissed per account.

Revision ID: 102
Revises: 101
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "102"
down_revision = "101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recurring_transactions", sa.Column("match_condition", postgresql.JSONB(), nullable=True))
    op.add_column(
        "recurring_transactions",
        sa.Column("amount_mode", sa.String(10), nullable=False, server_default="fixed"),
    )
    op.add_column(
        "recurring_transactions",
        sa.Column("amount_tolerance_pct", sa.Numeric(precision=5, scale=2), nullable=False, server_default="0"),
    )
    op.add_column("recurring_transactions", sa.Column("day_window", sa.SmallInteger(), nullable=True))
    op.add_column("recurring_transactions", sa.Column("billing_weekday", sa.SmallInteger(), nullable=True))
    op.add_column("recurring_transactions", sa.Column("week_count_basis", sa.String(20), nullable=True))
    op.add_column(
        "recurring_transactions",
        sa.Column("origin", sa.String(20), nullable=False, server_default="manual"),
    )
    op.add_column("recurring_transactions", sa.Column("import_ref", sa.String(100), nullable=True))
    op.create_index(
        "uq_recurring_transactions_workspace_import_ref",
        "recurring_transactions",
        ["workspace_id", "import_ref"],
        unique=True,
        postgresql_where=sa.text("import_ref IS NOT NULL"),
    )

    op.add_column("transactions", sa.Column("recurring_period", sa.Date(), nullable=True))
    op.add_column("transactions", sa.Column("recurring_link", sa.String(10), nullable=True))
    op.add_column("transactions", sa.Column("recurring_issue", sa.JSON(), nullable=True))
    op.add_column(
        "transactions",
        sa.Column("recurring_companion", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "transactions", sa.Column("recurring_manual_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "transactions_recurring_manual_id_fkey", "transactions", "recurring_transactions",
        ["recurring_manual_id"], ["id"], ondelete="SET NULL",
    )
    op.add_column(
        "transactions",
        sa.Column("recurring_manual_none", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "transactions",
        sa.Column("recurring_manual_excluded", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "transactions",
        sa.Column("recurring_rejected_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True),
    )

    op.create_table(
        "recurring_suggestion_dismissals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "key", name="uq_recurring_suggestion_dismissal"),
    )
    op.create_index(
        "ix_recurring_suggestion_dismissals_workspace_id", "recurring_suggestion_dismissals", ["workspace_id"]
    )
    op.create_index(
        "ix_recurring_suggestion_dismissals_account_id", "recurring_suggestion_dismissals", ["account_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_recurring_suggestion_dismissals_account_id", table_name="recurring_suggestion_dismissals")
    op.drop_index("ix_recurring_suggestion_dismissals_workspace_id", table_name="recurring_suggestion_dismissals")
    op.drop_table("recurring_suggestion_dismissals")
    op.drop_column("transactions", "recurring_rejected_ids")
    op.drop_column("transactions", "recurring_manual_excluded")
    op.drop_column("transactions", "recurring_manual_none")
    op.drop_constraint("transactions_recurring_manual_id_fkey", "transactions", type_="foreignkey")
    op.drop_column("transactions", "recurring_manual_id")
    op.drop_column("transactions", "recurring_companion")
    op.drop_column("transactions", "recurring_issue")
    op.drop_column("transactions", "recurring_link")
    op.drop_column("transactions", "recurring_period")
    op.drop_index("uq_recurring_transactions_workspace_import_ref", table_name="recurring_transactions")
    for name in (
        "import_ref", "origin", "week_count_basis", "billing_weekday", "day_window",
        "amount_tolerance_pct", "amount_mode", "match_condition",
    ):
        op.drop_column("recurring_transactions", name)
