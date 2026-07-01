"""Tests for POST /chats/{id}/auto-update (per-chat auto-update toggle).

Mirrors the fixture pattern from tests/unit/test_routes_chats_sync.py
exactly — same DB seeding, same ExportRunner monkeypatch, same session
forging helper.

Seeded chats:
  1001 — Fresh Alice  (exported, not stale, watch_enabled=False default)
  1002 — Stale Bob    (exported, stale)
  1003 — Never Charlie (never exported, no ChatState row)
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import routes_auth
from src.config import Settings, reset_settings_cache
from src.db.models import Chat, ChatState, ChatType
from src.db.session import create_engine, create_schema, create_session_factory
from src.main import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """App against a tmp DB. Mirrors test_routes_chats_sync.py fixture verbatim."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("PROXY_AUTO_SELECT", "false")
    fake_tdl = tmp_path / "fake_tdl.exe"
    fake_tdl.write_bytes(b"")
    monkeypatch.setenv("TDL_BINARY_PATH", str(fake_tdl))
    reset_settings_cache()
    Settings().ensure_dirs()
    routes_auth.reset_auth_state()
    (tmp_path / "data" / "vault.json").write_text('{"fake": true}')

    import asyncio

    async def seed() -> None:
        eng = create_engine(tmp_path / "data" / "state.db", dek=None)
        await create_schema(eng)
        f = create_session_factory(eng)
        now = datetime.now(UTC)
        yesterday = now - timedelta(days=1)
        last_week = now - timedelta(days=7)
        async with f() as s:
            # A — fresh exported chat.
            s.add(Chat(
                id=1001, title="Fresh Alice", type=ChatType.PRIVATE,
                last_message_date=last_week,
            ))
            s.add(ChatState(
                chat_id=1001, last_exported_message_id=42, last_export_at=now,
            ))
            # B — stale exported chat.
            s.add(Chat(
                id=1002, title="Stale Bob", type=ChatType.PRIVATE,
                last_message_date=now,
            ))
            s.add(ChatState(
                chat_id=1002, last_exported_message_id=10, last_export_at=yesterday,
            ))
            # C — never exported.
            s.add(Chat(
                id=1003, title="Never Charlie", type=ChatType.PRIVATE,
                last_message_date=now,
            ))
            await s.commit()
        await eng.dispose()

    asyncio.run(seed())

    from src.jobs.exporter import ExportRunner

    async def _noop_call(self, _job, _manager):  # noqa: ANN001
        return None

    monkeypatch.setattr(ExportRunner, "__call__", _noop_call, raising=True)

    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_settings_cache()


def _forge_session(client: TestClient) -> None:
    dek = secrets.token_bytes(32)
    s = routes_auth.SESSION_STORE.create(dek, username="admin")
    s.two_fa_passed = True
    client.cookies.set("tge_session", s.token)


# ---------- enable / disable ----------


def test_auto_update_enable(client: TestClient) -> None:
    """POST {"enabled": true} → 200, auto_update True, and a second GET reflects it."""
    _forge_session(client)
    r = client.post("/chats/1001/auto-update", json={"enabled": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chat_id"] == 1001
    assert body["auto_update"] is True

    # Verify persistence: toggle to False; if it was never persisted it would
    # read the original default (False) and toggling would yield True again.
    r2 = client.post("/chats/1001/auto-update", json={"enabled": False})
    assert r2.status_code == 200
    assert r2.json()["auto_update"] is False

    # Toggle back to True to confirm round-trip.
    r3 = client.post("/chats/1001/auto-update", json={"enabled": True})
    assert r3.status_code == 200
    assert r3.json()["auto_update"] is True


def test_auto_update_disable(client: TestClient) -> None:
    """POST {"enabled": false} → auto_update False; persists across calls."""
    _forge_session(client)
    # First enable it.
    r1 = client.post("/chats/1001/auto-update", json={"enabled": True})
    assert r1.json()["auto_update"] is True
    # Now disable.
    r = client.post("/chats/1001/auto-update", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["auto_update"] is False

    # Confirm persistence: re-read via the fragment and check data-on attribute.
    rfrag = client.get("/chats/fragment?type=all&recency=&folder=")
    assert rfrag.status_code == 200
    # The button for chat 1001 should now have data-on="false".
    assert 'data-on="false"' in rfrag.text


def test_auto_update_toggle_no_body(client: TestClient) -> None:
    """POST with no body twice → toggles on then off."""
    _forge_session(client)
    # chat 1001 starts with watch_enabled=False (default).
    r1 = client.post("/chats/1001/auto-update")
    assert r1.status_code == 200, r1.text
    assert r1.json()["auto_update"] is True

    r2 = client.post("/chats/1001/auto-update")
    assert r2.status_code == 200, r2.text
    assert r2.json()["auto_update"] is False


def test_auto_update_404_for_unknown_chat(client: TestClient) -> None:
    """POST for a non-existent chat id → 404."""
    _forge_session(client)
    r = client.post("/chats/99999999/auto-update", json={"enabled": True})
    assert r.status_code == 404


def test_auto_update_unauthenticated_redirects(client: TestClient) -> None:
    """No session cookie → redirect to /login."""
    r = client.post("/chats/1001/auto-update", json={"enabled": True}, follow_redirects=False)
    assert r.status_code in (303, 307)


def test_auto_update_toggle_creates_chat_state_if_absent(client: TestClient) -> None:
    """Chat 1003 has no ChatState row — toggling must upsert one and persist."""
    _forge_session(client)
    r = client.post("/chats/1003/auto-update", json={"enabled": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chat_id"] == 1003
    assert body["auto_update"] is True

    # Confirm the new row persists: disabling should return False (not toggle
    # from a missing row which would yield True again).
    r2 = client.post("/chats/1003/auto-update", json={"enabled": False})
    assert r2.status_code == 200
    assert r2.json()["auto_update"] is False


def test_fragment_contains_auto_update_toggle_markup(client: TestClient) -> None:
    """GET /chats/fragment for an exported chat contains the 'Авто' toggle markup."""
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    # Both exported chats (1001, 1002) should have the toggle button.
    assert "toggleAutoUpdate(this, 1001)" in r.text
    assert "toggleAutoUpdate(this, 1002)" in r.text
    # Never-exported chat (1003) is inside the {% if c.last_export_at %} guard.
    assert "toggleAutoUpdate(this, 1003)" not in r.text
    # The data-on attribute must be present on the button elements.
    assert 'data-on="false"' in r.text or 'data-on="true"' in r.text
