"""End-to-end auth route tests via FastAPI TestClient.

We patch `src.crypto.vault.create_vault`, `unlock_vault`, and
`change_password` to always run Argon2id at fast parameters so tests finish in
well under a second.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from src.api import routes_auth
from src.config import Settings, reset_settings_cache
from src.crypto import vault as vault_module
from src.main import create_app

FAST_KDF = {"memory_cost": 8192, "time_cost": 1, "parallelism": 1}
STRONG_PASSWORD = "Correct-Horse-Battery-Staple-9!"


@pytest.fixture
def fast_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force vault KDF parameters to low cost for speed."""
    import functools

    monkeypatch.setattr(
        vault_module,
        "create_vault",
        functools.partial(vault_module.create_vault, **FAST_KDF),
    )
    monkeypatch.setattr(
        vault_module,
        "change_password",
        functools.partial(vault_module.change_password, **FAST_KDF),
    )
    # Patch the already-imported binding inside routes_auth too.
    monkeypatch.setattr(
        routes_auth,
        "create_vault",
        functools.partial(vault_module.create_vault, **FAST_KDF),
    )
    monkeypatch.setattr(
        routes_auth,
        "vault_change_password",
        functools.partial(vault_module.change_password, **FAST_KDF),
    )


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fast_vault: None,
) -> Iterator[TestClient]:
    """Build an app whose settings point at tmp_path and return a TestClient."""
    data = tmp_path / "data"
    exports = tmp_path / "exports"
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("EXPORT_DIR", str(exports))
    reset_settings_cache()
    settings = Settings()
    settings.ensure_dirs()
    # Reset module-level singletons so tests don't leak state.
    routes_auth.reset_auth_state()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_settings_cache()


def _get(client: TestClient, url: str) -> object:
    return client.get(url, follow_redirects=False)


def _post(client: TestClient, url: str, data: dict) -> object:
    return client.post(url, data=data, follow_redirects=False)


def test_setup_get_renders_form(client: TestClient) -> None:
    r = _get(client, "/setup")
    assert r.status_code == 200
    assert "мастер-пароль" in r.text.lower() or "пароль" in r.text.lower()


def test_login_redirects_to_setup_when_no_vault(client: TestClient) -> None:
    r = _get(client, "/login")
    assert r.status_code in (303, 307)
    assert r.headers["location"] == "/setup"


def test_setup_rejects_weak_password(client: TestClient) -> None:
    r = _post(
        client,
        "/setup",
        {"password": "abc", "password_confirm": "abc"},
    )
    assert r.status_code == 400


def test_setup_rejects_mismatched_passwords(client: TestClient) -> None:
    r = _post(
        client,
        "/setup",
        {"password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD + "x"},
    )
    assert r.status_code == 400


def test_full_setup_to_login_round_trip(client: TestClient) -> None:
    # 1. Create vault.
    r = _post(
        client,
        "/setup",
        {"password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/setup/totp"
    cookie = client.cookies.get("tge_session")
    assert cookie

    # 2. Fetch TOTP page; grab secret.
    r = _get(client, "/setup/totp")
    assert r.status_code == 200
    session = routes_auth.SESSION_STORE.get(cookie)
    assert session is not None
    secret = session.pending_totp_secret
    assert secret

    # 3. Verify TOTP code.
    totp_code = pyotp.TOTP(secret).now()
    r = _post(client, "/setup/totp/verify", {"code": totp_code})
    assert r.status_code == 303
    assert r.headers["location"] == "/setup/backup-codes"

    # 4. Fetch + confirm backup codes.
    r = _get(client, "/setup/backup-codes")
    assert r.status_code == 200
    r = _post(client, "/setup/backup-codes/confirm", {"saved": "on"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    # 5. Now setup is done — /setup should redirect to /login.
    # But first we need a fresh client (no cookie).
    fresh = TestClient(client.app)
    r = fresh.get("/setup", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert r.headers["location"] == "/login"

    # 6. Login with the right password.
    r = fresh.post(
        "/login",
        data={"password": STRONG_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login/2fa"

    # 7. Submit valid TOTP for 2FA.
    totp_code = pyotp.TOTP(secret).now()
    r = fresh.post(
        "/login/2fa",
        data={"code": totp_code},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    # Session cookie is set and session is fully unlocked.
    token = fresh.cookies.get("tge_session")
    assert token
    s = routes_auth.SESSION_STORE.get(token)
    assert s is not None
    assert s.two_fa_passed is True


def test_login_wrong_password_rate_limits_after_five(client: TestClient) -> None:
    # Create a vault first.
    _post(
        client,
        "/setup",
        {"password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD},
    )
    # Drop the setup session cookie so login starts fresh.
    fresh = TestClient(client.app)

    # Five wrong attempts are rejected with 400.
    for _ in range(5):
        r = fresh.post(
            "/login",
            data={"password": "wrong"},
            follow_redirects=False,
        )
        assert r.status_code == 400

    # Sixth attempt is rate-limited.
    r = fresh.post(
        "/login",
        data={"password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 429


def test_lock_clears_session(client: TestClient) -> None:
    # Complete full setup first.
    _post(
        client,
        "/setup",
        {"password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD},
    )
    cookie = client.cookies.get("tge_session")
    _get(client, "/setup/totp")  # triggers pending_totp_secret generation
    session = routes_auth.SESSION_STORE.get(cookie)
    assert session is not None
    secret = session.pending_totp_secret
    assert secret
    _post(client, "/setup/totp/verify", {"code": pyotp.TOTP(secret).now()})
    _post(client, "/setup/backup-codes/confirm", {"saved": "on"})

    r = client.post("/lock", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # The session cookie is cleared and the old session is gone.
    assert routes_auth.SESSION_STORE.get(cookie) is None


def test_login_2fa_accepts_backup_code(client: TestClient) -> None:
    # Full setup.
    _post(
        client,
        "/setup",
        {"password": STRONG_PASSWORD, "password_confirm": STRONG_PASSWORD},
    )
    cookie = client.cookies.get("tge_session")
    _get(client, "/setup/totp")  # generates pending_totp_secret
    session = routes_auth.SESSION_STORE.get(cookie)
    assert session is not None
    secret = session.pending_totp_secret
    assert secret
    _post(client, "/setup/totp/verify", {"code": pyotp.TOTP(secret).now()})
    # Capture backup codes shown to the user.
    _get(client, "/setup/backup-codes")  # generates pending_backup_codes
    backup_codes = list(session.pending_backup_codes)
    assert len(backup_codes) == 10
    _post(client, "/setup/backup-codes/confirm", {"saved": "on"})

    # New session, log in, use backup code.
    fresh = TestClient(client.app)
    fresh.post("/login", data={"password": STRONG_PASSWORD}, follow_redirects=False)
    r = fresh.post(
        "/login/2fa",
        data={"code": backup_codes[0]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
