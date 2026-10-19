"""several Pluggy applications per instance (RFC toledo-features, Phase 1)

  - `pluggy_credentials` holds one Pluggy application (client id + secret)
    per row, both encrypted with CREDENTIALS_ENCRYPTION_KEY, plus a clear
    hint of the client id. Instance-global: no workspace.
  - `bank_connections.pluggy_credential_id` binds every Pluggy connection
    to the application that created its Item. RESTRICT keeps a credential
    in use from being deleted.

Existing connections start unbound; the backend binds them on startup to
the "Default" credential it creates from PLUGGY_CLIENT_ID/SECRET.

Revision ID: 097
Revises: 096
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "097"
down_revision = "096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pluggy_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("client_id_enc", sa.Text(), nullable=False),
        sa.Column("client_secret_enc", sa.Text(), nullable=False),
        sa.Column("client_id_hint", sa.String(length=20), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_pluggy_credentials_name_lower",
        "pluggy_credentials",
        [sa.text("lower(name)")],
        unique=True,
    )
    op.add_column(
        "bank_connections",
        sa.Column("pluggy_credential_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "bank_connections_pluggy_credential_id_fkey",
        "bank_connections",
        "pluggy_credentials",
        ["pluggy_credential_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_bank_connections_pluggy_credential_id",
        "bank_connections",
        ["pluggy_credential_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_bank_connections_pluggy_credential_id", table_name="bank_connections")
    op.drop_constraint(
        "bank_connections_pluggy_credential_id_fkey", "bank_connections", type_="foreignkey"
    )
    op.drop_column("bank_connections", "pluggy_credential_id")
    op.drop_index("uq_pluggy_credentials_name_lower", table_name="pluggy_credentials")
    op.drop_table("pluggy_credentials")
