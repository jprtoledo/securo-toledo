"""Rules v2 and classification provenance (RFC toledo-features, Phase 3)

  - rules: scope (account kind / one account) and the built-in flag with a
    stable key per workspace, so "Restore defaults" can find and recreate
    the shipped rules.
  - transactions: where the category came from (category_source,
    category_rule_id) for the sticky classification, and the derived card
    line kind (purchase / credit / payment / previous_balance) with its
    source and rule.

Existing categorized rows keep category_source null: recompute treats a
category of unknown provenance like a manual choice and never overrides it,
which is what rules did before (they never replaced an existing category).

Revision ID: 099
Revises: 098
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "099"
down_revision = "098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rules", sa.Column("scope_account_kind", sa.String(20), nullable=True))
    op.add_column("rules", sa.Column("scope_account_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "rules_scope_account_id_fkey", "rules", "accounts",
        ["scope_account_id"], ["id"], ondelete="SET NULL",
    )
    op.add_column(
        "rules", sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default="false")
    )
    op.add_column("rules", sa.Column("builtin_key", sa.String(64), nullable=True))
    op.create_index(
        "uq_rules_workspace_builtin_key", "rules", ["workspace_id", "builtin_key"],
        unique=True, postgresql_where=sa.text("builtin_key IS NOT NULL"),
    )

    op.add_column("transactions", sa.Column("category_source", sa.String(10), nullable=True))
    op.add_column(
        "transactions", sa.Column("category_rule_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "transactions_category_rule_id_fkey", "transactions", "rules",
        ["category_rule_id"], ["id"], ondelete="SET NULL",
    )
    op.add_column("transactions", sa.Column("card_line_kind", sa.String(20), nullable=True))
    op.add_column("transactions", sa.Column("card_line_kind_source", sa.String(10), nullable=True))
    op.add_column(
        "transactions",
        sa.Column("card_line_kind_rule_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "transactions_card_line_kind_rule_id_fkey", "transactions", "rules",
        ["card_line_kind_rule_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("transactions_card_line_kind_rule_id_fkey", "transactions", type_="foreignkey")
    op.drop_column("transactions", "card_line_kind_rule_id")
    op.drop_column("transactions", "card_line_kind_source")
    op.drop_column("transactions", "card_line_kind")
    op.drop_constraint("transactions_category_rule_id_fkey", "transactions", type_="foreignkey")
    op.drop_column("transactions", "category_rule_id")
    op.drop_column("transactions", "category_source")
    op.drop_index("uq_rules_workspace_builtin_key", table_name="rules")
    op.drop_column("rules", "builtin_key")
    op.drop_column("rules", "is_builtin")
    op.drop_constraint("rules_scope_account_id_fkey", "rules", type_="foreignkey")
    op.drop_column("rules", "scope_account_id")
    op.drop_column("rules", "scope_account_kind")
