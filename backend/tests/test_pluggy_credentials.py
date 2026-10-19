"""RFC toledo-features Phase 1 — multiple Pluggy credentials (AC-1.x)."""
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secrets_crypto
from app.core.config import get_settings
from app.models.bank_connection import BankConnection
from app.models.pluggy_credential import PluggyCredential
from app.providers.pluggy import PluggyAuthError, PluggyKeys, PluggyProvider
from app.services import connection_service, pluggy_credential_service

KEY = bytes(range(32))


# --- pure crypto ------------------------------------------------------------


def test_encrypt_round_trip_and_format():
    payload = secrets_crypto.encrypt("s3cr3t", "id-1:client_secret", KEY)
    assert payload.startswith("v1.")
    assert len(payload.split(".")) == 4
    assert secrets_crypto.decrypt(payload, "id-1:client_secret", KEY) == "s3cr3t"


def test_encrypt_uses_a_fresh_iv_every_time():
    a = secrets_crypto.encrypt("same", "ctx", KEY)
    b = secrets_crypto.encrypt("same", "ctx", KEY)
    assert a != b


def test_decrypt_fails_with_another_context():
    payload = secrets_crypto.encrypt("value", "id-1:client_id", KEY)
    with pytest.raises(secrets_crypto.DecryptionError):
        secrets_crypto.decrypt(payload, "id-2:client_id", KEY)
    with pytest.raises(secrets_crypto.DecryptionError):
        secrets_crypto.decrypt(payload, "id-1:client_secret", KEY)


def test_decrypt_fails_with_tampered_tag():
    version, iv, tag, ct = secrets_crypto.encrypt("value", "ctx", KEY).split(".")
    flipped = ("A" if tag[0] != "A" else "B") + tag[1:]
    with pytest.raises(secrets_crypto.DecryptionError):
        secrets_crypto.decrypt(".".join((version, iv, flipped, ct)), "ctx", KEY)


def test_decrypt_fails_with_another_key():
    payload = secrets_crypto.encrypt("value", "ctx", KEY)
    with pytest.raises(secrets_crypto.DecryptionError):
        secrets_crypto.decrypt(payload, "ctx", bytes(32))


def test_decrypt_rejects_unknown_format():
    with pytest.raises(secrets_crypto.DecryptionError):
        secrets_crypto.decrypt("v2.a.b.c", "ctx", KEY)


def test_hint():
    assert secrets_crypto.hint("abcd1234efgh5678") == "abcd…5678"
    assert secrets_crypto.hint("12345678") == "…"
    assert secrets_crypto.hint("") == "…"


def test_parse_key_accepts_base64_and_hex():
    import base64

    assert secrets_crypto.parse_key(base64.b64encode(KEY).decode()) == KEY
    assert secrets_crypto.parse_key(KEY.hex()) == KEY
    with pytest.raises(secrets_crypto.CredentialsKeyError, match="openssl rand"):
        secrets_crypto.parse_key("")
    with pytest.raises(secrets_crypto.CredentialsKeyError, match="32 bytes"):
        secrets_crypto.parse_key(base64.b64encode(b"short").decode())


# --- helpers ----------------------------------------------------------------


async def _pluggy_connection(session, user, credential=None, external_id="item-1"):
    conn = BankConnection(
        user_id=user.id,
        provider="pluggy",
        external_id=external_id,
        institution_name="Banco",
        credentials={"item_id": external_id},
        pluggy_credential_id=credential.id if credential else None,
    )
    session.add(conn)
    await session.commit()
    await session.refresh(conn)
    return conn


# --- AC-1.1 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_1_1_superuser_creates_credential_encrypted(
    client: AsyncClient, admin_auth_headers, session: AsyncSession
):
    resp = await client.post(
        "/api/admin/pluggy-credentials",
        json={"name": "Personal", "client_id": "abcd1234efgh5678", "client_secret": "top-secret"},
        headers=admin_auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["client_id_hint"] == "abcd…5678"
    assert "abcd1234efgh5678" not in resp.text
    assert "top-secret" not in resp.text
    assert "client_id" not in body and "client_secret" not in body

    row = (
        await session.execute(
            select(PluggyCredential).where(PluggyCredential.id == uuid.UUID(body["id"]))
        )
    ).scalar_one()
    assert row.client_id_enc.startswith("v1.")
    assert row.client_secret_enc.startswith("v1.")
    assert secrets_crypto.decrypt(row.client_id_enc, f"{row.id}:client_id") == "abcd1234efgh5678"
    with pytest.raises(secrets_crypto.DecryptionError):
        secrets_crypto.decrypt(row.client_id_enc, f"{uuid.uuid4()}:client_id")

    listed = await client.get("/api/admin/pluggy-credentials", headers=admin_auth_headers)
    assert listed.status_code == 200
    assert [c["name"] for c in listed.json()] == ["Personal"]
    assert "top-secret" not in listed.text


@pytest.mark.asyncio
async def test_duplicate_name_is_rejected_case_insensitively(
    client: AsyncClient, admin_auth_headers
):
    payload = {"name": "Personal", "client_id": "a" * 12, "client_secret": "b"}
    assert (await client.post("/api/admin/pluggy-credentials", json=payload, headers=admin_auth_headers)).status_code == 201
    payload["name"] = "PERSONAL"
    resp = await client.post("/api/admin/pluggy-credentials", json=payload, headers=admin_auth_headers)
    assert resp.status_code == 409


# --- AC-1.2 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_1_2_non_superusers_cannot_manage(
    client: AsyncClient, auth_headers, pluggy_credential
):
    cid = pluggy_credential.id
    calls = [
        client.get("/api/admin/pluggy-credentials", headers=auth_headers),
        client.get("/api/admin/pluggy-credentials/status", headers=auth_headers),
        client.post(
            "/api/admin/pluggy-credentials",
            json={"name": "x", "client_id": "y", "client_secret": "z"},
            headers=auth_headers,
        ),
        client.patch(f"/api/admin/pluggy-credentials/{cid}", json={"name": "x"}, headers=auth_headers),
        client.delete(f"/api/admin/pluggy-credentials/{cid}", headers=auth_headers),
    ]
    for call in calls:
        assert (await call).status_code == 403

    resp = await client.get("/api/connections/pluggy-credentials", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == [{"id": str(cid), "name": "Personal"}]


# --- AC-1.3 -----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttp:
    """Stands in for httpx.AsyncClient in the provider: records every call."""

    calls: list = []
    auth_status = 200

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, **kwargs):
        _FakeHttp.calls.append(("POST", url, json, headers))
        if url.endswith("/auth"):
            if _FakeHttp.auth_status >= 400:
                return _FakeResponse(_FakeHttp.auth_status, {"message": "Invalid credentials"})
            return _FakeResponse(200, {"apiKey": f"key-for-{json['clientId']}"})
        return _FakeResponse(200, {"accessToken": f"token-{headers['X-API-KEY']}"})

    async def get(self, url, headers=None, params=None, **kwargs):
        _FakeHttp.calls.append(("GET", url, params, headers))
        return _FakeResponse(200, {"results": []})


@pytest.fixture
def fake_http():
    _FakeHttp.calls = []
    _FakeHttp.auth_status = 200
    PluggyProvider._api_keys.clear()
    with patch("app.providers.pluggy.httpx.AsyncClient", _FakeHttp):
        yield _FakeHttp
    PluggyProvider._api_keys.clear()


@pytest.mark.asyncio
async def test_ac_1_3_no_validation_call_on_save_and_error_on_first_use(
    session: AsyncSession, test_user, test_workspace, fake_http
):
    from app.providers import register_provider

    register_provider("pluggy", PluggyProvider)
    credential = await pluggy_credential_service.create_credential(
        session, "Wrong", "wrong-client-id", "wrong-secret"
    )
    assert fake_http.calls == []  # nothing called on save

    conn = await _pluggy_connection(session, test_user, credential)
    fake_http.auth_status = 401
    with pytest.raises(PluggyAuthError):
        await connection_service.sync_connection(session, conn.id, conn.workspace_id, test_user.id)

    await session.refresh(credential)
    assert credential.last_error and "Invalid credentials" in credential.last_error
    refreshed = await session.get(BankConnection, conn.id)
    await session.refresh(refreshed)
    assert refreshed.status == "error"


# --- AC-1.4 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_1_4_each_connection_uses_its_own_credential(
    session: AsyncSession, test_user, test_workspace, fake_http
):
    from app.providers import register_provider

    register_provider("pluggy", PluggyProvider)
    a = await pluggy_credential_service.create_credential(session, "A", "client-a", "secret-a")
    b = await pluggy_credential_service.create_credential(session, "B", "client-b", "secret-b")
    x = await _pluggy_connection(session, test_user, a, "item-x")
    y = await _pluggy_connection(session, test_user, b, "item-y")

    for conn in (x, y, x):
        await connection_service.sync_connection(session, conn.id, conn.workspace_id, test_user.id)

    auths = [c for c in fake_http.calls if c[1].endswith("/auth")]
    # One /auth per credential: the second sync of X reuses A's cached key.
    assert [c[2]["clientId"] for c in auths] == ["client-a", "client-b"]
    item_calls = [c for c in fake_http.calls if c[0] == "GET"]
    for _, url, params, headers in item_calls:
        expected = "key-for-client-a" if params and params.get("itemId") == "item-x" else None
        if expected:
            assert headers["X-API-KEY"] == expected
        if params and params.get("itemId") == "item-y":
            assert headers["X-API-KEY"] == "key-for-client-b"
    assert set(PluggyProvider._api_keys) == {str(a.id), str(b.id)}


# --- AC-1.5 / AC-1.6 --------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_1_5_picker_required_with_two_credentials(
    client: AsyncClient, auth_headers, session: AsyncSession, fake_http
):
    from app.providers import register_provider

    register_provider("pluggy", PluggyProvider)
    a = await pluggy_credential_service.create_credential(session, "A", "client-a", "secret-a")
    await pluggy_credential_service.create_credential(session, "B", "client-b", "secret-b")

    resp = await client.post("/api/connections/connect-token", json={"provider": "pluggy"}, headers=auth_headers)
    assert resp.status_code == 400
    assert "Choose" in resp.json()["detail"]

    resp = await client.post(
        "/api/connections/connect-token",
        json={"provider": "pluggy", "pluggy_credential_id": str(a.id)},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "token-key-for-client-a"


@pytest.mark.asyncio
async def test_ac_1_5_single_credential_is_used_silently_and_bound(
    session: AsyncSession, test_user, test_workspace, pluggy_credential
):
    mock_provider = MagicMock()
    mock_provider.handle_oauth_callback = AsyncMock(
        return_value=MagicMock(
            external_id="item-new", institution_name="Bank", credentials={"item_id": "item-new"},
            accounts=[], logo_url=None,
        )
    )
    with patch("app.services.connection_service.get_provider", return_value=mock_provider) as gp:
        conn = await connection_service.handle_oauth_callback(
            session, test_workspace.id, test_user.id, "item-new", provider_name="pluggy",
        )
    assert conn.pluggy_credential_id == pluggy_credential.id
    keys = gp.call_args.kwargs["credential"]
    assert isinstance(keys, PluggyKeys)
    assert keys.client_id == "client-id-personal"


@pytest.mark.asyncio
async def test_ac_1_5_chosen_credential_is_bound(
    session: AsyncSession, test_user, test_workspace, pluggy_credential
):
    other = await pluggy_credential_service.create_credential(session, "Family", "client-fam", "s")
    mock_provider = MagicMock()
    mock_provider.handle_oauth_callback = AsyncMock(
        return_value=MagicMock(
            external_id="item-new", institution_name="Bank", credentials={"item_id": "item-new"},
            accounts=[], logo_url=None,
        )
    )
    with patch("app.services.connection_service.get_provider", return_value=mock_provider):
        with pytest.raises(pluggy_credential_service.CredentialRequired):
            await connection_service.handle_oauth_callback(
                session, test_workspace.id, test_user.id, "item-new", provider_name="pluggy",
            )
        conn = await connection_service.handle_oauth_callback(
            session, test_workspace.id, test_user.id, "item-new", provider_name="pluggy",
            pluggy_credential_id=other.id,
        )
    assert conn.pluggy_credential_id == other.id


@pytest.mark.asyncio
async def test_ac_1_6_reconnect_uses_bound_credential(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user, fake_http
):
    from app.providers import register_provider

    register_provider("pluggy", PluggyProvider)
    a = await pluggy_credential_service.create_credential(session, "A", "client-a", "secret-a")
    await pluggy_credential_service.create_credential(session, "B", "client-b", "secret-b")
    conn = await _pluggy_connection(session, test_user, a, "item-x")

    resp = await client.post(f"/api/connections/{conn.id}/reconnect-token", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    token_call = [c for c in fake_http.calls if c[1].endswith("/connect_token")][0]
    assert token_call[2] == {"clientUserId": str(test_user.id), "itemId": "item-x"}
    assert token_call[3]["X-API-KEY"] == "key-for-client-a"


# --- AC-1.7 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_1_7_bootstrap_from_env(
    session: AsyncSession, test_user, test_workspace, monkeypatch, caplog
):
    from pydantic import SecretStr

    settings = get_settings()
    monkeypatch.setattr(settings, "pluggy_client_id", "env-client-id-123")
    monkeypatch.setattr(settings, "pluggy_client_secret", SecretStr("env-secret"))
    c1 = await _pluggy_connection(session, test_user, None, "item-1")
    c2 = await _pluggy_connection(session, test_user, None, "item-2")

    created = await pluggy_credential_service.bootstrap_from_env(session)
    assert created is not None and created.name == "Default"
    keys = pluggy_credential_service.decrypt_keys(created)
    assert (keys.client_id, keys.client_secret) == ("env-client-id-123", "env-secret")
    for conn in (c1, c2):
        await session.refresh(conn)
        assert conn.pluggy_credential_id == created.id

    caplog.set_level(logging.WARNING)
    again = await pluggy_credential_service.bootstrap_from_env(session)
    assert again is None
    count = len((await session.execute(select(PluggyCredential))).scalars().all())
    assert count == 1
    assert "ignored" in caplog.text


@pytest.mark.asyncio
async def test_bootstrap_without_env_does_nothing(session: AsyncSession, clean_db, monkeypatch):
    from pydantic import SecretStr

    settings = get_settings()
    monkeypatch.setattr(settings, "pluggy_client_id", "")
    monkeypatch.setattr(settings, "pluggy_client_secret", SecretStr(""))
    assert await pluggy_credential_service.bootstrap_from_env(session) is None
    assert (await session.execute(select(PluggyCredential))).first() is None


# --- AC-1.8 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_1_8_delete_in_use_is_blocked(
    client: AsyncClient, admin_auth_headers, session: AsyncSession, test_user, test_workspace
):
    a = await pluggy_credential_service.create_credential(session, "A", "client-a", "secret-a")
    await _pluggy_connection(session, test_user, a, "item-1")
    await _pluggy_connection(session, test_user, a, "item-2")

    resp = await client.delete(f"/api/admin/pluggy-credentials/{a.id}", headers=admin_auth_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["connection_count"] == 2
    assert "2 connection" in resp.json()["detail"]["message"]
    assert await session.get(PluggyCredential, a.id) is not None

    listed = (await client.get("/api/admin/pluggy-credentials", headers=admin_auth_headers)).json()
    assert listed[0]["connection_count"] == 2


@pytest.mark.asyncio
async def test_delete_unused_credential(client: AsyncClient, admin_auth_headers, session: AsyncSession):
    a = await pluggy_credential_service.create_credential(session, "A", "client-a", "secret-a")
    resp = await client.delete(f"/api/admin/pluggy-credentials/{a.id}", headers=admin_auth_headers)
    assert resp.status_code == 204
    assert (await client.delete(f"/api/admin/pluggy-credentials/{a.id}", headers=admin_auth_headers)).status_code == 404


# --- AC-1.9 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_1_9_edit_keeps_secret_when_blank(
    client: AsyncClient, admin_auth_headers, session: AsyncSession
):
    a = await pluggy_credential_service.create_credential(session, "A", "client-a-12345", "secret-a")
    PluggyProvider._api_keys[str(a.id)] = ("v", "cached-key", 9e12)

    resp = await client.patch(
        f"/api/admin/pluggy-credentials/{a.id}",
        json={"name": "Renamed", "client_secret": ""},
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed"
    await session.refresh(a)
    keys = pluggy_credential_service.decrypt_keys(a)
    assert keys.client_secret == "secret-a"
    assert keys.client_id == "client-a-12345"
    assert str(a.id) not in PluggyProvider._api_keys

    resp = await client.patch(
        f"/api/admin/pluggy-credentials/{a.id}",
        json={"client_secret": "new-secret"},
        headers=admin_auth_headers,
    )
    await session.refresh(a)
    assert pluggy_credential_service.decrypt_keys(a).client_secret == "new-secret"


# --- AC-1.10 ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac_1_10_missing_encryption_key(
    client: AsyncClient, admin_auth_headers, monkeypatch
):
    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "credentials_encryption_key", SecretStr(""))
    resp = await client.post(
        "/api/admin/pluggy-credentials",
        json={"name": "A", "client_id": "client-a", "client_secret": "s"},
        headers=admin_auth_headers,
    )
    assert resp.status_code == 400
    assert "openssl rand -base64 32" in resp.json()["detail"]

    status = (await client.get("/api/admin/pluggy-credentials/status", headers=admin_auth_headers)).json()
    assert status["configured"] is False
    assert status["encryption_key_configured"] is False
    assert "CREDENTIALS_ENCRYPTION_KEY" in status["reason"]


@pytest.mark.asyncio
async def test_providers_endpoint_reports_pluggy_configured_from_credentials(
    client: AsyncClient, auth_headers, pluggy_credential
):
    from app.providers import register_provider

    register_provider("pluggy", PluggyProvider)
    data = (await client.get("/api/connections/providers", headers=auth_headers)).json()
    pluggy = next(p for p in data["providers"] if p["name"] == "pluggy")
    assert pluggy["configured"] is True


@pytest.mark.asyncio
async def test_unbound_connection_adopts_single_credential(
    session: AsyncSession, test_user, test_workspace, pluggy_credential
):
    conn = await _pluggy_connection(session, test_user, None)
    credential = await pluggy_credential_service.credential_for_connection(session, conn)
    assert credential.id == pluggy_credential.id
    assert conn.pluggy_credential_id == pluggy_credential.id
