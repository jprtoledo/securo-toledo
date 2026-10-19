"""Encryption of instance secrets stored in the database (Pluggy credentials).

AES-256-GCM with a dedicated key, ``CREDENTIALS_ENCRYPTION_KEY`` (32 bytes,
base64 or 64 hex chars). Every ciphertext is bound to a ``context`` passed as
AAD (e.g. ``"<credential id>:client_secret"``), so a value copied to another
row or column fails to decrypt instead of silently authenticating with the
wrong secret.

Storage format: ``v1.<iv>.<tag>.<ciphertext>``, each part base64url without
padding, 12-byte IV.

Deliberately separate from ``app.agents.services.crypto`` (Fernet derived from
SECRET_KEY): rotating the session signing key must not make the bank
credentials unreadable, and the key here can be backed up on its own.
"""
from __future__ import annotations

import base64
import binascii
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "v1"
KEY_HINT = "generate one with: openssl rand -base64 32"


class CredentialsKeyError(RuntimeError):
    """CREDENTIALS_ENCRYPTION_KEY is missing or malformed."""


class DecryptionError(ValueError):
    """A ciphertext could not be decrypted (wrong key, wrong context, tampered)."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parse_key(raw: str | None) -> bytes:
    """Decode the configured key. Raises CredentialsKeyError with a hint."""
    value = (raw or "").strip()
    if not value:
        raise CredentialsKeyError(f"CREDENTIALS_ENCRYPTION_KEY is not set ({KEY_HINT})")
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        key = bytes.fromhex(value)
    else:
        try:
            key = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            try:
                key = _b64d(value)
            except (binascii.Error, ValueError) as exc:
                raise CredentialsKeyError(
                    f"CREDENTIALS_ENCRYPTION_KEY is not valid base64 or hex ({KEY_HINT})"
                ) from exc
    if len(key) != 32:
        raise CredentialsKeyError(
            f"CREDENTIALS_ENCRYPTION_KEY must decode to 32 bytes, got {len(key)} ({KEY_HINT})"
        )
    return key


def configured_key() -> bytes:
    """The key from settings. Raises CredentialsKeyError when unusable."""
    from app.core.config import get_settings

    return parse_key(get_settings().credentials_encryption_key.get_secret_value())


def key_status() -> tuple[bool, str | None]:
    """(usable, reason) — for the admin page; never raises."""
    try:
        configured_key()
    except CredentialsKeyError as exc:
        return False, str(exc)
    return True, None


def encrypt(plain: str, context: str, key: bytes | None = None) -> str:
    key = key if key is not None else configured_key()
    iv = os.urandom(12)
    sealed = AESGCM(key).encrypt(iv, plain.encode("utf-8"), context.encode("utf-8"))
    # cryptography appends the 16-byte tag to the ciphertext.
    ciphertext, tag = sealed[:-16], sealed[-16:]
    return ".".join((VERSION, _b64e(iv), _b64e(tag), _b64e(ciphertext)))


def decrypt(payload: str, context: str, key: bytes | None = None) -> str:
    key = key if key is not None else configured_key()
    parts = (payload or "").split(".")
    if len(parts) != 4 or parts[0] != VERSION:
        raise DecryptionError("unknown encrypted secret format")
    try:
        iv, tag, ciphertext = (_b64d(p) for p in parts[1:])
        plain = AESGCM(key).decrypt(iv, ciphertext + tag, context.encode("utf-8"))
    except (InvalidTag, binascii.Error, ValueError) as exc:
        raise DecryptionError(
            "could not decrypt the secret — did CREDENTIALS_ENCRYPTION_KEY change?"
        ) from exc
    return plain.decode("utf-8")


def hint(value: str) -> str:
    """``abcd…wxyz``: recognizable on screen without exposing the value."""
    return "…" if len(value) <= 8 else f"{value[:4]}…{value[-4:]}"
