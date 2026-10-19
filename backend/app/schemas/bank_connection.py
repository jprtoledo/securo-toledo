import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class BankConnectionBase(BaseModel):
    provider: str
    institution_name: str


class ConnectionInstitutionRead(BaseModel):
    """One institution reached through a connection (issue #345). Distinct
    from InstitutionRead below, which is a provider's connectable-bank
    catalog entry, not a linked institution."""

    name: str
    logo_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class BankConnectionRead(BankConnectionBase):
    id: uuid.UUID
    user_id: uuid.UUID
    external_id: str
    display_name: Optional[str] = None
    logo_url: Optional[str] = None
    settings: Optional[dict] = None
    status: str
    last_sync_at: Optional[datetime] = None
    created_at: datetime
    # Pluggy: the application the connection authenticates with.
    pluggy_credential_id: Optional[uuid.UUID] = None
    # Provider-side item snapshot (informational; RFC toledo-features §4.3).
    provider_status: Optional[str] = None
    provider_execution_status: Optional[str] = None
    provider_last_updated_at: Optional[datetime] = None
    consent_expires_at: Optional[datetime] = None
    # Institutions this link spans. Empty for providers that are one
    # institution per connection — institution_name above covers those.
    institutions: list[ConnectionInstitutionRead] = []

    model_config = ConfigDict(from_attributes=True)


class OAuthUrlRequest(BaseModel):
    provider: str = "pluggy"
    flow_params: Optional[dict] = None


class OAuthUrlResponse(BaseModel):
    url: str


class OAuthCallbackRequest(BaseModel):
    code: str
    state: Optional[str] = None
    provider: Optional[str] = None
    sync_assets: Optional[bool] = None
    reconnect_connection_id: Optional[uuid.UUID] = None
    # Pluggy: the application the widget's connect token was created with.
    pluggy_credential_id: Optional[uuid.UUID] = None


class ReauthUrlResponse(BaseModel):
    url: str


class InstitutionRead(BaseModel):
    name: str
    display_name: str
    country: str
    logo: Optional[str] = None
    bic: Optional[str] = None
    psu_types: list[str] = []
    max_consent_days: Optional[int] = None
    max_history_days: Optional[int] = None


class InstitutionListResponse(BaseModel):
    countries: list[str]
    institutions: list[InstitutionRead]


class ConnectTokenRequest(BaseModel):
    provider: str = "pluggy"
    # Required for Pluggy when more than one application exists.
    pluggy_credential_id: Optional[uuid.UUID] = None


class PluggyCredentialOption(BaseModel):
    """What any member sees of a Pluggy application: never the keys."""

    id: uuid.UUID
    name: str


class ConnectTokenResponse(BaseModel):
    access_token: str


class ReconnectTokenResponse(BaseModel):
    access_token: str


class ConnectionSettingsUpdate(BaseModel):
    display_name: Optional[str] = None
    payee_source: Optional[Literal["auto", "merchant", "payment_data", "description", "none"]] = None
    import_pending: Optional[bool] = None
    sync_assets: Optional[bool] = None
