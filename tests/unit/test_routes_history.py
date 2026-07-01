"""Tests for /history routes (view + file).

The /view route should redirect to the bundled TG-Desktop-style editor
with ``?src=`` pointing at /history/{id}/file/messages.html, so the
chat list can double-click into a working preview without exposing the
filesystem to the user.

The /file route serves any file out of the chat folder (relative paths
only, no traversal), used by the editor to fetch messages.html and the
attached media.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import routes_auth
from src.config import Settings, reset_settings_cache
from src.main import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    data = tmp_path / "data"
    exports = tmp_path / "exports"
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("EXPORT_DIR", str(exports))
    reset_settings_cache()
    Settings().ensure_dirs()
    routes_auth.reset_auth_state()
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


def _make_chat_dir(tmp_path: Path, chat_id: int, *, title: str | None = None) -> Path:
    # Reproduce the exporter's slug logic. The /history routes look up
    # the title via the Chat table — when no Chat row exists, the
    # resolver passes title=None, so by default we mirror that here so
    # the test exercises the no-DB-row code path.
    from src.jobs.exporter import _chat_slug

    chat_dir = tmp_path / "exports" / _chat_slug(chat_id, title)
    chat_dir.mkdir(parents=True, exist_ok=True)
    (chat_dir / "messages.html").write_text(
        '<!DOCTYPE html><html><body><div class="page_wrap">'
        '<div class="page_body chat_page"><div class="history">'
        '<div class="message default clearfix" id="message1">'
        '<div class="body">hi</div></div>'
        '</div></div></div></body></html>',
        encoding="utf-8",
    )
    (chat_dir / "messages.json").write_text(
        json.dumps({"id": chat_id, "messages": [
            {"id": 1, "date": 1700000000, "from_id": 100, "text": "hi"},
        ]}),
        encoding="utf-8",
    )
    return chat_dir


def test_view_redirects_to_editor(client: TestClient, tmp_path: Path) -> None:
    _forge_session(client)
    _make_chat_dir(tmp_path, chat_id=12345)

    r = client.get("/history/12345/view", follow_redirects=False)
    assert r.status_code == 303, f"expected 303 redirect, got {r.status_code}"
    location = r.headers["location"]
    assert "/static/tg_export/editor.html" in location
    assert "src=" in location
    # The src= param must point at the /history/{id}/file/messages.html route.
    assert "%2Fhistory%2F12345%2Ffile%2Fmessages.html" in location \
        or "/history/12345/file/messages.html" in location


def test_view_raw_serves_html_directly(client: TestClient, tmp_path: Path) -> None:
    _forge_session(client)
    _make_chat_dir(tmp_path, chat_id=12345)

    r = client.get("/history/12345/view?raw=1", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "page_wrap" in r.text


def test_view_friendly_page_when_no_messages_html(
    client: TestClient, tmp_path: Path
) -> None:
    """Used to be a 404, but ``_install_friendly_error_handlers`` in
    main.py turns HTML 404s into a parent-path redirect chain that
    ultimately lands on ``/`` — so double-clicking a never-exported
    chat looked like "redirected to main menu". The route now returns
    HTTP 200 with an inline "не экспортирован" page instead, so the
    new tab stays put."""
    _forge_session(client)
    # No chat dir exists — should serve a friendly 200 page.
    r = client.get("/history/99999/view", follow_redirects=False)
    assert r.status_code == 200
    body = r.text.lower()
    assert "ещё не экспортирован" in body
    assert "/chats" in body


def test_file_serves_messages_html(client: TestClient, tmp_path: Path) -> None:
    _forge_session(client)
    _make_chat_dir(tmp_path, chat_id=12345)

    r = client.get("/history/12345/file/messages.html")
    assert r.status_code == 200
    assert "page_wrap" in r.text


def test_file_blocks_path_traversal(client: TestClient, tmp_path: Path) -> None:
    _forge_session(client)
    _make_chat_dir(tmp_path, chat_id=12345)

    # Try to escape via ../../etc/passwd
    r = client.get("/history/12345/file/..%2F..%2Fetc%2Fpasswd")
    # Either 403 (blocked at parent escape) or 404 (file doesn't exist outside).
    assert r.status_code in (403, 404)


def test_file_404_for_missing_path(client: TestClient, tmp_path: Path) -> None:
    _forge_session(client)
    _make_chat_dir(tmp_path, chat_id=12345)

    r = client.get("/history/12345/file/nonexistent.html")
    assert r.status_code == 404


def test_view_unauth_redirects_to_login(client: TestClient) -> None:
    r = client.get("/history/12345/view", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert r.headers["location"] == "/login"
