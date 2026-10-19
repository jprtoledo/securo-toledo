"""Installment plans (RFC toledo-features, Phase 5)

  - installment_plans: one purchase split in parcels — detected by the
    recompute heuristic (stable per account + key), created by a user
    decision (manual), or exposing a manual installment series (series).
  - transactions: the derived link to the plan (installment_plan_id,
    installment_link, installment_issue) and the user's decision
    (installment_plan_manual_id), which recompute never re-evaluates.

Revision ID: 101
Revises: 100
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "101"
down_revision = "100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "installment_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("key", sa.String(300), nullable=True),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column("total_installments", sa.SmallInteger(), nullable=False),
        sa.Column("installment_amount", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("first_month", sa.Date(), nullable=False),
        sa.Column("purchase_date", sa.Date(), nullable=True),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_installment_plans_workspace_id", "installment_plans", ["workspace_id"])
    op.create_index("ix_installment_plans_account_id", "installment_plans", ["account_id"])
    op.create_index(
        "uq_installment_plans_account_key", "installment_plans", ["account_id", "key"],
        unique=True, postgresql_where=sa.text("key IS NOT NULL"),
    )
    op.add_column("transactions", sa.Column("installment_plan_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("transactions", sa.Column("installment_link", sa.String(10), nullable=True))
    op.add_column("transactions", sa.Column("installment_issue", sa.JSON(), nullable=True))
    op.add_column(
        "transactions", sa.Column("installment_plan_manual_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "transactions_installment_plan_id_fkey", "transactions", "installment_plans",
        ["installment_plan_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "transactions_installment_plan_manual_id_fkey", "transactions", "installment_plans",
        ["installment_plan_manual_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_transactions_installment_plan_id", "transactions", ["installment_plan_id"])


def downgrade() -> None:
    op.drop_index("ix_transactions_installment_plan_id", table_name="transactions")
    op.drop_constraint("transactions_installment_plan_manual_id_fkey", "transactions", type_="foreignkey")
    op.drop_constraint("transactions_installment_plan_id_fkey", "transactions", type_="foreignkey")
    op.drop_column("transactions", "installment_plan_manual_id")
    op.drop_column("transactions", "installment_issue")
    op.drop_column("transactions", "installment_link")
    op.drop_column("transactions", "installment_plan_id")
    op.drop_index("uq_installment_plans_account_key", table_name="installment_plans")
    op.drop_index("ix_installment_plans_account_id", table_name="installment_plans")
    op.drop_index("ix_installment_plans_workspace_id", table_name="installment_plans")
    op.drop_table("installment_plans")
