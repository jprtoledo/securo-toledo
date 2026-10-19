import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PluggyCredential(Base):
    """One Pluggy application (client id + secret from dashboard.pluggy.ai).

    Instance-global, managed by superusers: an Item (connection) is only
    readable by the application that created it, so every Pluggy connection
    is bound to the credential it was created with
    (bank_connections.pluggy_credential_id). The keys are stored encrypted
    with CREDENTIALS_ENCRYPTION_KEY (app.core.secrets_crypto); the context
    of each ciphertext is "<id>:client_id" / "<id>:client_secret".
    """

    __tablename__ = "pluggy_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    client_id_enc: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    # "abcd…wxyz", stored in clear so the admin can recognize the key.
    client_id_hint: Mapped[str] = mapped_column(String(20), nullable=False)
    # Last authentication failure seen while using the credential; cleared
    # by the next successful sync or by editing the keys.
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("uq_pluggy_credentials_name_lower", func.lower(name), unique=True),
    )
