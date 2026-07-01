"""End-to-end tests for ``/auth/telegram/*`` routes.

The Telethon client never actually connects: we monkeypatch
``TelegramSessionManager.start_login``, ``submit_code``, ``submit_password``,
``is_authorized``, and ``close`` so every test stays offline.

We also forge a logged-in session by calling ``SESSION_STORE.create`` directly
and flipping ``two_fa_passed`` — the real setup/login flow is covered by
``tests/unit/test_routes_auth.py``.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import routes_auth
from src.config import Settings, reset_settings_cache
from src.main import create_app
from src.telegram import telethon_client as tc_module


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    data = tmp_path / "data"
    exports = tmp_path / "exports"
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("EXPORT_DIR", str(exports))
    reset_settings_cache()
    settings = Settings()
    settings.ensure_dirs()
    routes_auth.reset_auth_state()

    # Make a fake vault file so middleware doesn't redirect to /setup.
    (data / "vault.json").write_text('{"fake": true}')

    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_settings_cache()


def _forge_session(client: TestClient) -> tuple[str, bytes]:
    """Create a logged-in session, bypassing password+TOTP. Returns token+DEK."""
    dek = secrets.token_bytes(32)
    session = routes_auth.SESSION_STORE.create(dek, username="admin")
    session.two_fa_passed = True
    client.cookies.set("tge_session", session.token)
    return session.token, dek


def test_unauth_dashboard_redirects_to_login(client: TestClient) -> None:
    r = client.get("/auth/telegram", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert r.headers["location"] == "/login"


def test_unauth_credentials_post_redirects_to_login(client: TestClient) -> None:
    r = client.post(
        "/auth/telegram/credentials",
        data={"api_id": "1", "api_hash": "x"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 307)
    assert r.headers["location"] == "/login"


def test_logged_in_dashboard_renders(client: TestClient) -> None:
    _forge_session(client)
    r = client.get("/auth/telegram", follow_redirects=False)
    assert r.status_code == 200
    assert "Подключение Telegram" in r.text
    # Credentials card visible.
    assert "api_id" in r.text


def test_credentials_persist(client: TestClient) -> None:
    _forge_session(client)

    r = client.post(
        "/auth/telegram/credentials",
        data={"api_id": "1234", "api_hash": "x" * 32},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/telegram"

    # GET the dashboard and verify creds are now shown as configured.
    r = client.get("/auth/telegram", follow_redirects=False)
    assert r.status_code == 200
    assert "настроено" in r.text
    assert "1234" in r.text


def test_dashboard_seeds_credentials_from_env_on_first_visit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If TELEGRAM_API_ID and _HASH live in .env but the encrypted DB has
    no row yet, the very first GET /auth/telegram should silently persist
    them and show the dashboard as 'configured'."""
    data = tmp_path / "data"
    exports = tmp_path / "exports"
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("EXPORT_DIR", str(exports))
    monkeypatch.setenv("TELEGRAM_API_ID", "987654")
    monkeypatch.setenv("TELEGRAM_API_HASH", "z" * 32)
    reset_settings_cache()
    settings = Settings()
    settings.ensure_dirs()
    routes_auth.reset_auth_state()
    (data / "vault.json").write_text('{"fake": true}')

    app = create_app()
    with TestClient(app) as c:
        dek = secrets.token_bytes(32)
        session = routes_auth.SESSION_STORE.create(dek, username="admin")
        session.two_fa_passed = True
        c.cookies.set("tge_session", session.token)

        r = c.get("/auth/telegram", follow_redirects=False)
        assert r.status_code == 200
        # api_id from env shows up; status is 'configured', not 'required'.
        assert "987654" in r.text
        assert "настроено" in r.text

        # And it actually persisted: a second GET (no env required) still configured.
        monkeypatch.delenv("TELEGRAM_API_ID")
        monkeypatch.delenv("TELEGRAM_API_HASH")
        reset_settings_cache()
        r2 = c.get("/auth/telegram", follow_redirects=False)
        assert r2.status_code == 200
        assert "настроено" in r2.text
    reset_settings_cache()


def test_credentials_rejects_non_int_api_id(client: TestClient) -> None:
    _forge_session(client)
    r = client.post(
        "/auth/telegram/credentials",
        data={"api_id": "not-an-int", "api_hash": "x" * 32},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "api_id" in r.text


def test_start_without_credentials_shows_friendly_error(client: TestClient) -> None:
    _forge_session(client)
    r = client.post(
        "/auth/telegram/start",
        data={"phone": "+491701234567"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "api_id" in r.text.lower() or "api_hash" in r.text.lower()


def test_start_rejects_non_e164_phone(client: TestClient) -> None:
    _forge_session(client)
    # Need creds first.
    client.post(
        "/auth/telegram/credentials",
        data={"api_id": "1", "api_hash": "x" * 32},
        follow_redirects=False,
    )
    r = client.post(
        "/auth/telegram/start",
        data={"phone": "1234"},  # no leading + → rejected
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "E.164" in r.text or "формат" in r.text


def test_full_flow_phone_code_then_ready(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, dek = _forge_session(client)

    # Stub all Telethon-touching methods.
    async def fake_start_login(self, phone):  # noqa: ANN001
        self._pending_phone = phone
        self._pending_phone_hash = "fakehash"
        return tc_module.AuthStep.CODE

    async def fake_submit_code(self, code):  # noqa: ANN001
        return tc_module.AuthStep.READY

    async def fake_is_authorized(self):  # noqa: ANN001
        return True

    async def fake_close(self):  # noqa: ANN001
        return None

    monkeypatch.setattr(tc_module.TelegramSessionManager, "start_login", fake_start_login)
    monkeypatch.setattr(tc_module.TelegramSessionManager, "submit_code", fake_submit_code)
    monkeypatch.setattr(tc_module.TelegramSessionManager, "is_authorized", fake_is_authorized)
    monkeypatch.setattr(tc_module.TelegramSessionManager, "close", fake_close)

    # Save creds.
    client.post(
        "/auth/telegram/credentials",
        data={"api_id": "9", "api_hash": "h" * 32},
        follow_redirects=False,
    )

    # Phone step.
    r = client.post(
        "/auth/telegram/start",
        data={"phone": "+491701234567"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Код подтверждения" in r.text

    # Code step → success → /chats.
    r = client.post(
        "/auth/telegram/code",
        data={"code": "12345"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/chats"

    # Pending map cleared.
    pending = getattr(client.app.state, "telegram_pending", {})
    assert token not in pending


def test_full_flow_phone_code_2fa_then_ready(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, _ = _forge_session(client)

    async def fake_start_login(self, phone):  # noqa: ANN001
        self._pending_phone = phone
        self._pending_phone_hash = "fakehash"
        return tc_module.AuthStep.CODE

    async def fake_submit_code(self, code):  # noqa: ANN001
        return tc_module.AuthStep.PASSWORD

    async def fake_submit_password(self, password):  # noqa: ANN001
        return tc_module.AuthStep.READY

    async def fake_close(self):  # noqa: ANN001
        return None

    monkeypatch.setattr(tc_module.TelegramSessionManager, "start_login", fake_start_login)
    monkeypatch.setattr(tc_module.TelegramSessionManager, "submit_code", fake_submit_code)
    monkeypatch.setattr(tc_module.TelegramSessionManager, "submit_password", fake_submit_password)
    monkeypatch.setattr(tc_module.TelegramSessionManager, "close", fake_close)

    client.post(
        "/auth/telegram/credentials",
        data={"api_id": "7", "api_hash": "k" * 32},
        follow_redirects=False,
    )
    client.post(
        "/auth/telegram/start",
        data={"phone": "+491701234567"},
        follow_redirects=False,
    )

    # Code step → 2FA form.
    r = client.post(
        "/auth/telegram/code",
        data={"code": "98765"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Облачный пароль" in r.text

    # Password step → /chats redirect.
    r = client.post(
        "/auth/telegram/password",
        data={"password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/chats"

    pending = getattr(client.app.state, "telegram_pending", {})
    assert token not in pending


def test_start_surfaces_telegram_auth_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forge_session(client)

    async def boom(self, phone):  # noqa: ANN001
        raise tc_module.TelegramAuthError("phone_invalid", "phone looks bad")

    async def fake_close(self):  # noqa: ANN001
        return None

    monkeypatch.setattr(tc_module.TelegramSessionManager, "start_login", boom)
    monkeypatch.setattr(tc_module.TelegramSessionManager, "close", fake_close)

    client.post(
        "/auth/telegram/credentials",
        data={"api_id": "1", "api_hash": "z" * 32},
        follow_redirects=False,
    )
    r = client.post(
        "/auth/telegram/start",
        data={"phone": "+491701234567"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "phone looks bad" in r.text


def test_disconnect_removes_manager_and_session_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token, _ = _forge_session(client)

    # Seed a manager on app.state directly.
    async def fake_close(self):  # noqa: ANN001
        return None

    monkeypatch.setattr(tc_module.TelegramSessionManager, "close", fake_close)

    # Touch the expected session file so we can assert it's unlinked.
    from src.api.routes_telegram import _session_file_path

    session_path = _session_file_path(token)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_bytes(b"{}")
    assert session_path.exists()

    # Plant a real manager so the app.state check fires.
    from src.config import get_settings

    settings = get_settings()
    mgr = tc_module.TelegramSessionManager(
        api_id=1,
        api_hash="x" * 32,
        session_path=session_path,
        dek=secrets.token_bytes(32),
        proxy=settings.proxy,
    )
    client.app.state.telegram_manager = mgr

    r = client.post("/auth/telegram/disconnect", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/telegram"

    assert not hasattr(client.app.state, "telegram_manager")
    assert not session_path.exists()
