import uuid
from typing import TYPE_CHECKING

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, JSON, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.user import User


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    conditions_op: Mapped[str] = mapped_column(String(3), default="and")  # "and" | "or"
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Scope (RFC toledo-features §5.3): null = every account; 'bank'
    # (checking/savings) or 'credit_card'. scope_account_id narrows to one
    # account and forces the kind to that account's.
    scope_account_kind: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    scope_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    # Shipped by Securo (e.g. the Brazilian credit-card pack). Editable and
    # disableable; "Restore defaults" recreates them from builtin_key.
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    builtin_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    user: Mapped["User"] = relationship()

    __table_args__ = (
        Index(
            "uq_rules_workspace_builtin_key",
            "workspace_id",
            "builtin_key",
            unique=True,
            postgresql_where=text("builtin_key IS NOT NULL"),
            sqlite_where=text("builtin_key IS NOT NULL"),
        ),
    )
