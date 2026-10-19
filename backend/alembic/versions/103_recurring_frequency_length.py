"""Widen recurring_transactions.frequency for weekly_billed_monthly

"weekly_billed_monthly" (21 chars, RFC toledo-features Phase 6) does not
fit the original String(20).

Revision ID: 103
Revises: 102
"""
from alembic import op
import sqlalchemy as sa

revision = "103"
down_revision = "102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "recurring_transactions",
        "frequency",
        type_=sa.String(32),
        existing_type=sa.String(20),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "recurring_transactions",
        "frequency",
        type_=sa.String(20),
        existing_type=sa.String(32),
        existing_nullable=False,
    )
