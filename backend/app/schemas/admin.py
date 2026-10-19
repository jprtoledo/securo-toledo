import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class AdminUserRead(BaseModel):
    id: uuid.UUID
    email: str
    is_active: bool
    is_superuser: bool
    is_verified: bool
    preferences: Optional[dict] = None

    model_config = ConfigDict(from_attributes=True)


class AdminUserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    is_superuser: bool = False
    preferences: Optional[dict] = None


class AdminUserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(None, min_length=8, max_length=72)
    is_active: Optional[bool] = None
    is_superuser: Optional[bool] = None
    preferences: Optional[dict] = None


class AdminUserList(BaseModel):
    items: list[AdminUserRead]
    total: int


class AppSettingRead(BaseModel):
    key: str
    value: str

    model_config = ConfigDict(from_attributes=True)


class AppSettingUpdate(BaseModel):
    value: str


class TimezoneSettingRead(BaseModel):
    #: The timezone in use, after fallbacks.
    timezone: str
    #: What an administrator saved, valid or not; None when nothing is saved.
    saved: Optional[str] = None
    #: Where the application lands without a saved value (TZ, host, UTC).
    fallback: str
    available: list[str]


class PluggyCredentialRead(BaseModel):
    """A Pluggy application as the admin page sees it. Never carries the
    client id or the secret — only the clear hint of the client id."""

    id: uuid.UUID
    name: str
    client_id_hint: str
    connection_count: int = 0
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class PluggyCredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str = Field(min_length=1, max_length=500)


class PluggyCredentialUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    # Blank or omitted = keep the stored value.
    client_id: Optional[str] = Field(None, max_length=500)
    client_secret: Optional[str] = Field(None, max_length=500)


class PluggyStatusRead(BaseModel):
    configured: bool
    encryption_key_configured: bool
    reason: Optional[str] = None
    credential_count: int
