"""Recurrences paid from more than one account

A recurrence may be paid by any of several accounts, one whole charge per
period: ``account_id`` stays the account its forecast lands on and
``extra_account_ids`` lists the other accounts that may pay it.

Revision ID: 105
Revises: 104
"""
from alembic import op
import sqlalchemy as sa

revision = "105"
down_revision = "104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recurring_transactions", sa.Column("extra_account_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("recurring_transactions", "extra_account_ids")
