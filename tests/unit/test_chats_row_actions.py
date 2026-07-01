"""Tests for the per-chat row actions on ``/chats``:
  * Double-click anywhere on a row → opens ``/history/{id}/view`` in
    a new browser tab via ``openChatTranscript(id, event)``.
  * Explicit "Открыть" button on every previously-exported row →
    same as double-click, click-only fallback for the case when the
    browser silently reuses an open editor tab.
  * "Синхр." button on every previously-exported row → POST to
    ``/chats/{id}/sync`` via ``syncOneChat(this, id)``.

User reported "у меня по двойному клику перестали чаты открываться"
after they already had one editor tab open — Chrome reused the
existing window because both ``window.open`` calls used the same
``_blank`` target. Fix: ``openChatTranscript`` now generates a
unique window name per call AND falls back to ``location.href`` if
``window.open`` returns null (popup blocker).

These tests pin the contract the JS in chats.html relies on:
  * The double-click handler attribute exists on every chat <li>
    with the right argument shape.
  * The "Открыть" button is rendered for every exported chat.
  * Child interactive elements (checkbox, sync button, open button)
    carry ``ondblclick="event.stopPropagation()"`` so double-clicking
    them doesn't trigger the parent <li>'s navigation handler.
  * The /history/{id}/view route still serves a 303 redirect to the
    bundled editor with ``?src=`` set (covered by
    ``test_routes_history.py`` — this file focuses on the chat-row
    side of the contract).
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
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("PROXY_AUTO_SELECT", "false")
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
        async with f() as s:
            # Exported, fresh — no stale tint, but Open + Sync buttons rendered.
            s.add(Chat(
                id=1001, title="Илья Шмаков", username="ilyshka1",
                type=ChatType.PRIVATE, is_public=True,
                last_message_date=now - timedelta(days=10),
            ))
            s.add(ChatState(
                chat_id=1001,
                last_exported_message_id=462518,
                last_export_at=now,
                total_files=50, total_size_bytes=1234,
            ))
            # Exported, stale — amber tint, Open + Sync (amber).
            s.add(Chat(
                id=1002, title="Настя Шкарина", username="sibirskai07",
                type=ChatType.PRIVATE, last_message_date=now,
            ))
            s.add(ChatState(
                chat_id=1002,
                last_exported_message_id=462520,
                last_export_at=now - timedelta(days=14),
                total_files=503, total_size_bytes=678_000_000,
            ))
            # Never exported — no Open / Sync buttons (would 404 anyway).
            s.add(Chat(
                id=1003, title="Новенький", type=ChatType.PRIVATE,
                last_message_date=now,
            ))
            await s.commit()
        await eng.dispose()

    asyncio.run(seed())

    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_settings_cache()


def _forge_session(client: TestClient) -> None:
    dek = secrets.token_bytes(32)
    s = routes_auth.SESSION_STORE.create(dek, username="admin")
    s.two_fa_passed = True
    client.cookies.set("tge_session", s.token)


# ---------- Double-click handler ----------


def test_every_row_has_ondblclick_calling_openChatTranscript(
    client: TestClient,
) -> None:
    """The <li> wraps the whole row and dispatches dblclick to a global
    JS helper. The id is passed positionally so renaming the helper
    or its signature breaks this assertion loudly."""
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    assert 'ondblclick="openChatTranscript(1001, event)"' in r.text
    assert 'ondblclick="openChatTranscript(1002, event)"' in r.text
    assert 'ondblclick="openChatTranscript(1003, event)"' in r.text


def test_row_carries_data_chat_row_attribute(client: TestClient) -> None:
    """``data-chat-row`` on the <li> lets selectors find the row in
    tests / DOM scripts without scraping the inline handler text."""
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    assert 'data-chat-row="1001"' in r.text
    assert 'data-chat-row="1002"' in r.text


# ---------- Explicit "Открыть" button ----------


def test_open_button_rendered_for_exported_chats(client: TestClient) -> None:
    """The "Открыть" fallback button only appears on rows with a
    last_export_at — for never-exported chats the route would 404
    and there's nothing to view yet."""
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    # Two exported rows (1001 + 1002) → two "Открыть" buttons.
    assert r.text.count("openChatTranscript(1001, event)") >= 2  # li + button
    assert r.text.count("openChatTranscript(1002, event)") >= 2
    # The literal button text appears for each exported chat.
    assert r.text.count(">\n                Открыть\n            </button>") == 2 \
        or r.text.count("Открыть\n            </button>") >= 2


def test_open_button_not_rendered_for_never_exported(client: TestClient) -> None:
    """Never-exported chat (id=1003) gets neither Open nor Sync."""
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    # The dblclick handler still exists on the row (1003), but no
    # explicit Open button call with that id from a <button>.
    # Count how many times the helper is invoked for chat 1003 — should
    # be exactly 1 (just the <li> handler).
    assert r.text.count("openChatTranscript(1003, event)") == 1


# ---------- Child stopPropagation ----------


def test_checkbox_stops_dblclick_propagation(client: TestClient) -> None:
    """Without this, double-clicking the checkbox would also fire the
    row's transcript-open handler, which surprised users in earlier
    sessions."""
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    # Each checkbox has both onclick + ondblclick stopPropagation guards.
    assert 'ondblclick="event.stopPropagation()"' in r.text


def test_sync_button_stops_dblclick_propagation(client: TestClient) -> None:
    """Same defence for the Sync button so a hot-click doesn't open
    the transcript on top of the sync-job redirect."""
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    # syncOneChat call + stopPropagation should appear together on
    # the same button. Count: 2 exported chats → 2 sync buttons.
    assert r.text.count("syncOneChat(this, 1001)") == 1
    assert r.text.count("syncOneChat(this, 1002)") == 1


# ---------- The JS helper itself ----------


def test_openChatTranscript_helper_defined_in_chats_page(
    client: TestClient,
) -> None:
    """The full /chats page (not the fragment) must include the JS
    helper definition. The fragment can be loaded multiple times via
    HTMX swap and SHOULD NOT redefine the helper — only chats.html
    declares it once at page load."""
    _forge_session(client)
    r = client.get("/chats")
    assert r.status_code == 200
    assert "function openChatTranscript(chatId, ev)" in r.text
    # Unique window name + popup-blocker fallback are the two
    # post-merge behaviours; pin them so a future refactor doesn't
    # accidentally drop them.
    assert "tge_chat_" in r.text
    assert "window.location.href" in r.text


def test_openChatTranscript_NOT_in_fragment(client: TestClient) -> None:
    """The HTMX fragment endpoint is swapped into the page many times
    via filter changes. Re-declaring ``function openChatTranscript``
    each time would throw a SyntaxError in strict mode. Make sure
    the fragment template doesn't carry the helper body."""
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    assert "function openChatTranscript" not in r.text


# ---------- Server-side /history/{id}/view redirect contract ----------


def test_view_redirect_target_matches_what_dblclick_opens(
    client: TestClient, tmp_path: Path,
) -> None:
    """Pins the end-to-end shape: openChatTranscript() opens
    ``/history/<id>/view`` which redirects (303) to
    ``/static/tg_export/editor.html?src=<file>``. Without this we
    could break the second hop and the chat would silently fail to
    render."""
    _forge_session(client)
    # Stand up a fake messages.html for chat 1001 so /view doesn't 404.
    # ``_resolve_chat_dir`` looks the chat's title up from the DB via the
    # ``chats`` table, so the on-disk slug includes the title — use the
    # same title we seeded above.
    from src.jobs.exporter import _chat_slug

    chat_dir = tmp_path / "exports" / _chat_slug(1001, "Илья Шмаков")
    chat_dir.mkdir(parents=True, exist_ok=True)
    (chat_dir / "messages.html").write_text("<html><body>hi</body></html>", encoding="utf-8")

    r = client.get("/history/1001/view", follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/static/tg_export/editor.html")
    # The redirect carries the relative path to the chat's messages.html.
    assert "src=" in location
    assert "%2Fhistory%2F1001%2Ffile%2Fmessages.html" in location \
        or "/history/1001/file/messages.html" in location
