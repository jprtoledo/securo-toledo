"""Recurrence occurrences the user marked as not happening

Reports count every recurrence still to come; an occurrence that will not
happen (or did not) is skipped by hand, per period.

Revision ID: 104
Revises: 103
"""
from alembic import op
import sqlalchemy as sa

revision = "104"
down_revision = "103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recurring_transactions", sa.Column("skipped_periods", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("recurring_transactions", "skipped_periods")
