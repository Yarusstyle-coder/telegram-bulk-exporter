"""Tests for ``/avatars/{chat_id}`` — serves cached JPEGs, falls back to a 1x1 PNG."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import routes_auth
from src.config import Settings, reset_settings_cache
from src.main import create_app


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

    # Fake vault so session middleware doesn't redirect to /setup.
    (data / "vault.json").write_text('{"fake": true}')

    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_settings_cache()


def _forge_session(client: TestClient) -> None:
    dek = secrets.token_bytes(32)
    session = routes_auth.SESSION_STORE.create(dek, username="admin")
    session.two_fa_passed = True
    client.cookies.set("tge_session", session.token)


def test_unauth_redirects_to_login(client: TestClient) -> None:
    r = client.get("/avatars/1001", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert r.headers["location"] == "/login"


def test_missing_avatar_returns_transparent_png(client: TestClient) -> None:
    _forge_session(client)
    r = client.get("/avatars/999999", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    # PNG magic bytes.
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert r.headers["cache-control"] == "private, max-age=3600"


def test_existing_avatar_is_served(client: TestClient, tmp_path: Path) -> None:
    _forge_session(client)

    from src.config import get_settings

    settings = get_settings()
    avatar_path = settings.avatars_dir / "1234.jpg"
    avatar_path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal valid JPEG-ish bytes for the round-trip assertion.
    fake_bytes = b"\xff\xd8\xff\xe0" + b"fake avatar body" + b"\xff\xd9"
    avatar_path.write_bytes(fake_bytes)

    r = client.get("/avatars/1234", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == fake_bytes
    assert r.headers["cache-control"] == "private, max-age=3600"
