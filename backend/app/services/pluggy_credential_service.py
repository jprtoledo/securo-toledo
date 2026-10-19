"""Pluggy applications (credentials) stored in the database — RFC Phase 1.

Every Pluggy Item is readable only by the application that created it, so
each Pluggy connection is bound to one credential and every call made for it
authenticates with that credential's keys. The keys are encrypted at rest
(app.core.secrets_crypto) and never leave the backend: the API returns the
name and a hint of the client id only.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secrets_crypto
from app.core.config import get_settings
from app.models.bank_connection import BankConnection
from app.models.pluggy_credential import PluggyCredential
from app.providers.base import ProviderNotConfiguredError
from app.providers.pluggy import PluggyKeys, PluggyProvider

logger = logging.getLogger(__name__)

DEFAULT_NAME = "Default"


class CredentialError(ValueError):
    """Invalid input for a credential (maps to HTTP 400)."""


class CredentialNameTaken(CredentialError):
    """Another credential already uses this name (case-insensitive)."""


class CredentialNotFound(LookupError):
    pass


class CredentialInUse(RuntimeError):
    def __init__(self, count: int) -> None:
        super().__init__(
            f"This Pluggy application is used by {count} connection(s); "
            "remove or move them before deleting it"
        )
        self.count = count


class CredentialRequired(CredentialError):
    """Several credentials exist and the caller did not say which one."""


@dataclass
class CredentialSummary:
    credential: PluggyCredential
    connection_count: int


def _contexts(credential_id: uuid.UUID) -> tuple[str, str]:
    return f"{credential_id}:client_id", f"{credential_id}:client_secret"


def _version(credential: PluggyCredential) -> str:
    return credential.updated_at.isoformat() if credential.updated_at else ""


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


async def _name_taken(
    session: AsyncSession, name: str, exclude_id: Optional[uuid.UUID] = None
) -> bool:
    query = select(PluggyCredential.id).where(func.lower(PluggyCredential.name) == name.lower())
    if exclude_id is not None:
        query = query.where(PluggyCredential.id != exclude_id)
    return (await session.execute(query.limit(1))).first() is not None


async def connection_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(BankConnection.pluggy_credential_id, func.count())
        .where(BankConnection.pluggy_credential_id.is_not(None))
        .group_by(BankConnection.pluggy_credential_id)
    )
    return {cid: n for cid, n in rows.all()}


async def list_credentials(session: AsyncSession) -> list[CredentialSummary]:
    credentials = (
        await session.execute(select(PluggyCredential).order_by(func.lower(PluggyCredential.name)))
    ).scalars().all()
    counts = await connection_counts(session)
    return [CredentialSummary(c, counts.get(c.id, 0)) for c in credentials]


async def get_credential(session: AsyncSession, credential_id: uuid.UUID) -> PluggyCredential:
    credential = await session.get(PluggyCredential, credential_id)
    if credential is None:
        raise CredentialNotFound("Pluggy application not found")
    return credential


async def create_credential(
    session: AsyncSession,
    name: str,
    client_id: str,
    client_secret: str,
    *,
    commit: bool = True,
) -> PluggyCredential:
    """Store a credential. No call to Pluggy is made (AC-1.3): wrong keys
    surface on first use, in ``last_error``."""
    name, client_id, client_secret = _clean(name), _clean(client_id), _clean(client_secret)
    if not name:
        raise CredentialError("Name is required")
    if not client_id or not client_secret:
        raise CredentialError("Client id and client secret are required")
    key = secrets_crypto.configured_key()  # raises CredentialsKeyError (AC-1.10)
    if await _name_taken(session, name):
        raise CredentialNameTaken(f"A Pluggy application named '{name}' already exists")

    credential_id = uuid.uuid4()
    id_ctx, secret_ctx = _contexts(credential_id)
    credential = PluggyCredential(
        id=credential_id,
        name=name,
        client_id_enc=secrets_crypto.encrypt(client_id, id_ctx, key),
        client_secret_enc=secrets_crypto.encrypt(client_secret, secret_ctx, key),
        client_id_hint=secrets_crypto.hint(client_id),
    )
    session.add(credential)
    if commit:
        await session.commit()
        await session.refresh(credential)
    else:
        await session.flush()
    return credential


async def update_credential(
    session: AsyncSession,
    credential_id: uuid.UUID,
    *,
    name: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> PluggyCredential:
    """Rename and/or replace keys. A blank client id or secret keeps the
    stored one (AC-1.9). Any change discards the cached API key."""
    credential = await get_credential(session, credential_id)
    if name is not None:
        name = _clean(name)
        if not name:
            raise CredentialError("Name is required")
        if await _name_taken(session, name, exclude_id=credential.id):
            raise CredentialNameTaken(f"A Pluggy application named '{name}' already exists")
        credential.name = name

    client_id, client_secret = _clean(client_id), _clean(client_secret)
    if client_id or client_secret:
        key = secrets_crypto.configured_key()
        id_ctx, secret_ctx = _contexts(credential.id)
        if client_id:
            credential.client_id_enc = secrets_crypto.encrypt(client_id, id_ctx, key)
            credential.client_id_hint = secrets_crypto.hint(client_id)
        if client_secret:
            credential.client_secret_enc = secrets_crypto.encrypt(client_secret, secret_ctx, key)
        credential.last_error = None

    PluggyProvider.invalidate_api_key(credential.id)
    await session.commit()
    await session.refresh(credential)
    return credential


async def delete_credential(session: AsyncSession, credential_id: uuid.UUID) -> None:
    credential = await get_credential(session, credential_id)
    count = (await connection_counts(session)).get(credential.id, 0)
    if count:
        raise CredentialInUse(count)
    PluggyProvider.invalidate_api_key(credential.id)
    await session.delete(credential)
    await session.commit()


def decrypt_keys(credential: PluggyCredential) -> PluggyKeys:
    """Decrypted keys of a credential. Raises ProviderNotConfiguredError when
    the encryption key is missing or no longer matches."""
    id_ctx, secret_ctx = _contexts(credential.id)
    try:
        key = secrets_crypto.configured_key()
        return PluggyKeys(
            credential_id=str(credential.id),
            client_id=secrets_crypto.decrypt(credential.client_id_enc, id_ctx, key),
            client_secret=secrets_crypto.decrypt(credential.client_secret_enc, secret_ctx, key),
            version=_version(credential),
        )
    except (secrets_crypto.CredentialsKeyError, secrets_crypto.DecryptionError) as exc:
        raise ProviderNotConfiguredError(
            f"Pluggy application '{credential.name}' cannot be used: {exc}. "
            "Re-enter its keys in the Admin page."
        ) from exc


async def _single_credential(session: AsyncSession) -> Optional[PluggyCredential]:
    rows = (await session.execute(select(PluggyCredential).limit(2))).scalars().all()
    return rows[0] if len(rows) == 1 else None


async def credential_for_connection(
    session: AsyncSession, connection: BankConnection
) -> PluggyCredential:
    """The credential a Pluggy connection must use. An unbound connection
    adopts the only credential when there is exactly one (legacy rows)."""
    if connection.pluggy_credential_id is not None:
        credential = await session.get(PluggyCredential, connection.pluggy_credential_id)
        if credential is not None:
            return credential
    credential = await _single_credential(session)
    if credential is None:
        raise ProviderNotConfiguredError(
            "This Pluggy connection is not bound to a Pluggy application. "
            "An administrator must add one in the Admin page."
        )
    connection.pluggy_credential_id = credential.id
    return credential


async def credential_for_new_item(
    session: AsyncSession, credential_id: Optional[uuid.UUID]
) -> PluggyCredential:
    """The credential to create a new Item with: the one asked for, or the
    only one there is (AC-1.5)."""
    if credential_id is not None:
        credential = await session.get(PluggyCredential, credential_id)
        if credential is None:
            raise CredentialError("Pluggy application not found")
        return credential
    count = (await session.execute(select(func.count()).select_from(PluggyCredential))).scalar_one()
    if count == 0:
        raise CredentialError("Pluggy is not configured: no Pluggy application exists")
    if count > 1:
        raise CredentialRequired("Choose which Pluggy application to connect with")
    return (await session.execute(select(PluggyCredential))).scalar_one()


async def record_auth_error(
    session: AsyncSession, credential_id: uuid.UUID | str, message: str
) -> None:
    await session.execute(
        update(PluggyCredential)
        .where(PluggyCredential.id == uuid.UUID(str(credential_id)))
        .values(last_error=message[:2000])
    )


async def clear_auth_error(session: AsyncSession, credential: PluggyCredential) -> None:
    if credential.last_error is not None:
        credential.last_error = None


async def status(session: AsyncSession) -> dict:
    """What the admin page shows about Pluggy as a whole (AC-1.10)."""
    usable, reason = secrets_crypto.key_status()
    count = (await session.execute(select(func.count()).select_from(PluggyCredential))).scalar_one()
    return {
        "configured": usable and count > 0,
        "encryption_key_configured": usable,
        "reason": reason if not usable else (None if count else "No Pluggy application yet"),
        "credential_count": count,
    }


async def is_configured(session: AsyncSession) -> bool:
    return (await status(session))["configured"]


async def bootstrap_from_env(session: AsyncSession) -> Optional[PluggyCredential]:
    """Create "Default" from PLUGGY_CLIENT_ID/SECRET when no credential exists
    and bind every unbound Pluggy connection to it (AC-1.7). Idempotent.

    With credentials already stored, env keys are ignored with a warning.
    """
    settings = get_settings()
    env_id = settings.pluggy_client_id.strip()
    env_secret = settings.pluggy_client_secret.get_secret_value().strip()
    existing = (await session.execute(select(PluggyCredential))).scalars().all()

    created: Optional[PluggyCredential] = None
    if not existing and env_id and env_secret:
        try:
            created = await create_credential(
                session, DEFAULT_NAME, env_id, env_secret, commit=False
            )
        except secrets_crypto.CredentialsKeyError as exc:
            logger.warning(
                "PLUGGY_CLIENT_ID/SECRET are set but cannot be stored: %s", exc
            )
            return None
        existing = [created]
        logger.info(
            "Pluggy credential from the environment stored as '%s'; "
            "PLUGGY_CLIENT_ID/SECRET can now be removed", DEFAULT_NAME,
        )
    elif existing and (env_id or env_secret):
        logger.warning(
            "PLUGGY_CLIENT_ID/SECRET are ignored: Pluggy applications are managed "
            "in the Admin page"
        )

    if len(existing) == 1:
        result = await session.execute(
            update(BankConnection)
            .where(
                BankConnection.provider == "pluggy",
                BankConnection.pluggy_credential_id.is_(None),
            )
            .values(pluggy_credential_id=existing[0].id)
        )
        if result.rowcount:
            logger.info(
                "Bound %d Pluggy connection(s) to the Pluggy application '%s'",
                result.rowcount, existing[0].name,
            )
    elif existing:
        orphans = (
            await session.execute(
                select(func.count()).select_from(BankConnection).where(
                    BankConnection.provider == "pluggy",
                    BankConnection.pluggy_credential_id.is_(None),
                )
            )
        ).scalar_one()
        if orphans:
            logger.warning(
                "%d Pluggy connection(s) are not bound to a Pluggy application", orphans
            )
    await session.commit()
    return created
