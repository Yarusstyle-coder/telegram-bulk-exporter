"""Tests for the quick-sync routes (POST /chats/{id}/sync and
POST /chats/sync-stale) + the staleness flag the chat-list loader
emits per row.

Staleness rule: a chat is "stale" when it has been exported before
(``ChatState.last_exported_message_id is not None`` and
``last_export_at is not None``) AND there's a fresher message in
Telegram than the last export (``chats.last_message_date >
chat_states.last_export_at``).

The sync endpoints submit a Job to the JobManager with sensible
defaults (``only_new=True``, all media types) so the user doesn't
have to dig through the export modal for "I just want to catch up".
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
    """App against a tmp DB. We seed three chats:
      A — exported, fresh   (last_msg < last_export → not stale)
      B — exported, stale   (last_msg > last_export)
      C — never exported    (no ChatState row)
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("PROXY_AUTO_SELECT", "false")
    # ``composition.attach_runtime`` constructs a ``TdlWrapper`` which
    # asserts the binary exists. Without a stub file a checkout with no
    # ``tools/tdl/tdl.exe`` makes the runtime attach silently
    # skip and ``app.state.job_manager`` stays unset — every test that
    # POSTs to /chats/{id}/sync or /chats/sync-stale gets back HTTP 503
    # "Job manager not available". The stub is enough for unit tests
    # because ``ExportRunner.__call__`` is monkeypatched to a no-op
    # below, so tdl is never actually invoked.
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
            # A — fresh: last export at "now", last message a week ago.
            s.add(Chat(
                id=1001, title="Fresh Alice", type=ChatType.PRIVATE,
                last_message_date=last_week,
            ))
            s.add(ChatState(
                chat_id=1001, last_exported_message_id=42, last_export_at=now,
            ))
            # B — stale: last export yesterday, but a message came in today.
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

    # Replace ``ExportRunner.__call__`` with a no-op coroutine BEFORE
    # the app starts up, so the JobManager wires the stubbed runner
    # at composition time. Without this, on a dev box that has a real
    # ``tools/tdl/tdl.exe`` the submitted job actually starts a tdl
    # child and the TestClient teardown hangs waiting for the export
    # task to finish. (The route + JobManager.submit() wiring is what
    # these tests cover — the runner itself has its own tests.)
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


# ---------- Staleness flag through /chats/fragment ----------


def test_fragment_marks_stale_chat_with_alert_icon(client: TestClient) -> None:
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    # Stale row uses the amber alert path; non-stale exported row uses
    # the green check path. Both should be present in the rendered grid.
    assert "data-chat-id=\"1002\"" in r.text
    # Stale row carries the amber dot + 'есть новые после' label.
    assert "есть новые после" in r.text
    # Fresh row carries 'синхронизирован до #42'.
    assert "синхронизирован до #42" in r.text


def test_fragment_per_chat_sync_button_only_for_exported(
    client: TestClient,
) -> None:
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    # Sync button is wired with syncOneChat(this, <id>).
    assert "syncOneChat(this, 1001)" in r.text  # fresh-exported
    assert "syncOneChat(this, 1002)" in r.text  # stale
    assert "syncOneChat(this, 1003)" not in r.text  # never exported


def test_sync_status_endpoint_returns_live_percent(client: TestClient) -> None:
    """``GET /chats/{id}/sync-status`` returns the JobManager's live
    in-memory percent for the freshest job touching ``chat_id``. This
    is what the chats-page poller uses to keep "⟳ Идёт N%" updating
    without a full page reload."""
    _forge_session(client)
    from collections import deque

    from src.db.models import JobStatus
    from src.jobs.job_manager import Job
    from src.jobs.models import JobSettings

    mgr = client.app.state.job_manager
    fake = Job(
        id="live-1",
        settings=JobSettings(chat_ids=[1002]),
        status=JobStatus.DOWNLOADING,
        percent=42.5,
        current=85,
        total=200,
    )
    mgr._jobs[fake.id] = fake
    mgr._ring[fake.id] = deque(maxlen=10)
    mgr._subs[fake.id] = set()
    try:
        r = client.get("/chats/1002/sync-status")
        assert r.status_code == 200
        body = r.json()
        assert body["in_progress"] is True
        assert body["status"] == "downloading"
        assert body["percent"] == 42.5
        assert body["current"] == 85
        assert body["total"] == 200
        assert body["job_id"] == "live-1"
    finally:
        mgr._jobs.pop(fake.id, None)
        mgr._ring.pop(fake.id, None)
        mgr._subs.pop(fake.id, None)


def test_sync_filter_pills_buckets(client: TestClient) -> None:
    """``GET /chats?sync=…`` slices the universe by sync lifecycle:
    ``synced`` returns the fresh-exported chats only, ``stale``
    returns the ones with newer messages since export, ``never``
    returns the unexported ones, ``syncing`` keys off a live job.

    The fixture seeds:
        1001 — Fresh Alice  (exported, NOT stale)
        1002 — Stale Bob    (exported, IS stale)
        1003 — Never Charlie (never exported)
    """
    _forge_session(client)
    r = client.get("/chats?sync=synced")
    assert r.status_code == 200
    assert "1001" in r.text
    assert 'data-chat-id="1002"' not in r.text
    assert 'data-chat-id="1003"' not in r.text

    r = client.get("/chats?sync=stale")
    assert r.status_code == 200
    assert 'data-chat-id="1002"' in r.text
    assert 'data-chat-id="1001"' not in r.text
    assert 'data-chat-id="1003"' not in r.text

    r = client.get("/chats?sync=never")
    assert r.status_code == 200
    assert 'data-chat-id="1003"' in r.text
    assert 'data-chat-id="1001"' not in r.text
    assert 'data-chat-id="1002"' not in r.text


def test_sync_filter_syncing_keys_off_running_job(client: TestClient) -> None:
    """``?sync=syncing`` must show ONLY the chats with an active
    JobManager job — even when those chats would otherwise be
    "synced" (fresh) or "never" (no DB state)."""
    _forge_session(client)
    from collections import deque

    from src.db.models import JobStatus
    from src.jobs.job_manager import Job
    from src.jobs.models import JobSettings

    mgr = client.app.state.job_manager
    fake = Job(
        id="sync-filter-test",
        settings=JobSettings(chat_ids=[1003]),  # never-exported
        status=JobStatus.EXPORTING,
    )
    mgr._jobs[fake.id] = fake
    mgr._ring[fake.id] = deque(maxlen=10)
    mgr._subs[fake.id] = set()
    try:
        r = client.get("/chats?sync=syncing")
        assert r.status_code == 200
        # 1003 has no ChatState but DOES have an active job →
        # qualifies for "syncing". Other chats are filtered out.
        assert 'data-chat-id="1003"' in r.text
        assert 'data-chat-id="1001"' not in r.text
        assert 'data-chat-id="1002"' not in r.text
    finally:
        mgr._jobs.pop(fake.id, None)
        mgr._ring.pop(fake.id, None)
        mgr._subs.pop(fake.id, None)


def test_delete_chat_removes_db_row_and_cascades(
    client: TestClient, tmp_path: Path,
) -> None:
    """POST /chats/{id}/delete drops the chats row + cascades the
    children. The export folder under EXPORT_DIR is also removed
    (with all media), but the dedup ``media_pool`` is untouched
    because that's content-addressed shared storage."""
    _forge_session(client)
    # Seed an export folder on disk to make sure rmtree path is
    # exercised + the response counters report the right numbers.
    export_root = tmp_path / "exports"
    chat_dir = export_root / "chat_Stale_Bob_1002"
    (chat_dir / "media").mkdir(parents=True)
    (chat_dir / "media" / "fake.bin").write_bytes(b"x" * 1024)
    (chat_dir / "messages.json").write_text("{}", encoding="utf-8")

    r = client.post("/chats/1002/delete")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] is True
    assert body["title"] == "Stale Bob"
    assert body["removed_files"] == 2  # fake.bin + messages.json
    assert body["freed_bytes"] > 1000  # at least 1 KB of media
    # Folder is gone.
    assert not chat_dir.exists()
    # /chats no longer renders the chat.
    r2 = client.get("/chats")
    assert 'data-chat-id="1002"' not in r2.text


def test_delete_chat_idempotent_on_missing(client: TestClient) -> None:
    """Deleting a chat that doesn't exist returns ``{deleted: false}``
    with HTTP 200 so the UI can fire the request without 404-handling
    races against another tab."""
    _forge_session(client)
    r = client.post("/chats/999999999/delete")
    assert r.status_code == 200
    assert r.json() == {"deleted": False, "removed_files": 0, "freed_bytes": 0}


def test_delete_chat_blocks_while_syncing(client: TestClient) -> None:
    """A mid-flight sync job for the chat must block the delete —
    otherwise we'd be yanking a folder out from under a running
    tdl child. HTTP 409 surfaces in the JS handler as a friendly
    alert and the row stays in place."""
    _forge_session(client)
    from collections import deque

    from src.db.models import JobStatus
    from src.jobs.job_manager import Job
    from src.jobs.models import JobSettings

    mgr = client.app.state.job_manager
    fake = Job(
        id="block-delete",
        settings=JobSettings(chat_ids=[1002]),
        status=JobStatus.DOWNLOADING,
    )
    mgr._jobs[fake.id] = fake
    mgr._ring[fake.id] = deque(maxlen=10)
    mgr._subs[fake.id] = set()
    try:
        r = client.post("/chats/1002/delete")
        assert r.status_code == 409
        assert "синхрониз" in r.text.lower()
        # Row still in /chats.
        r2 = client.get("/chats")
        assert 'data-chat-id="1002"' in r2.text
    finally:
        mgr._jobs.pop(fake.id, None)
        mgr._ring.pop(fake.id, None)
        mgr._subs.pop(fake.id, None)


def test_fragment_emits_oob_sync_count_swaps(client: TestClient) -> None:
    """HTMX swaps the grid into ``#chats-grid`` and leaves the pill
    row's count badges untouched — without an OOB swap on each
    ``sync-count-<slug>`` span, the user sees "С новыми 1" with an
    empty filtered grid after a sync completes. This test pins the
    OOB-swap markers so a future template refactor breaks it loudly
    rather than silently re-introducing the stale-counter bug."""
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=&min_messages=")
    assert r.status_code == 200
    # Every bucket has an OOB swap that updates the parent's pill.
    for slug in ("all", "synced", "stale", "syncing", "never"):
        token = f'id="sync-count-{slug}"'
        assert token in r.text
        # Make sure the OOB attribute is present on the SAME element.
        idx = r.text.index(token)
        nearby = r.text[idx : idx + 200]
        assert 'hx-swap-oob="true"' in nearby, f"OOB missing on {slug}: {nearby!r}"


def test_sync_status_endpoint_returns_idle_when_no_job(client: TestClient) -> None:
    """No job in flight → ``in_progress: false, status: "idle"``.
    The poller uses this signal to reload /chats and let the row
    swap back to stale-or-fresh rendering."""
    _forge_session(client)
    r = client.get("/chats/1001/sync-status")
    assert r.status_code == 200
    body = r.json()
    assert body["in_progress"] is False
    assert body["status"] == "idle"


def test_fragment_renders_idet_button_when_sync_in_progress(
    client: TestClient,
) -> None:
    """When a chat has a mid-flight sync job in JobManager, the row
    must render a disabled "⟳ Идёт…" pill INSTEAD of the orange
    "Синхр." button — otherwise the user clicks Sync, watches their
    job grind for minutes, comes back to /chats, and reasonably
    wonders why the button hasn't changed colour. ``last_export_at``
    only advances after the whole pipeline finishes, so without this
    annotation the row stays "stale orange" the whole time."""
    _forge_session(client)
    # Simulate a running sync job for chat 1002 (the stale one).
    import asyncio as _asyncio

    from src.api.routes_jobs import router as _  # noqa: F401 — ensure module loaded
    from src.db.models import JobStatus
    from src.jobs.job_manager import Job, JobManager
    from src.jobs.models import JobSettings

    mgr: JobManager = client.app.state.job_manager
    fake_job = Job(
        id="fake-running-job",
        settings=JobSettings(chat_ids=[1002]),
        status=JobStatus.RUNNING,
    )
    mgr._jobs[fake_job.id] = fake_job
    mgr._ring[fake_job.id] = __import__("collections").deque(maxlen=10)
    mgr._subs[fake_job.id] = set()
    try:
        r = client.get("/chats/fragment?type=all&recency=&folder=")
        assert r.status_code == 200
        # The disabled "Идёт N%" pill is present, percent comes from
        # the in-memory job snapshot (defaults to 0 when the runner
        # hasn't emitted PROGRESS yet).
        assert "Идёт" in r.text
        assert 'cursor-wait' in r.text
        assert 'data-syncing-chat-id="1002"' in r.text
        assert 'data-sync-percent' in r.text
        # The orange Sync button for 1002 is NOT rendered — only the
        # spinning placeholder. (The handler for 1001 — the fresh
        # chat — should still be there since no job touches it.)
        assert "syncOneChat(this, 1002)" not in r.text
        assert "syncOneChat(this, 1001)" in r.text
    finally:
        mgr._jobs.pop(fake_job.id, None)
        mgr._ring.pop(fake_job.id, None)
        mgr._subs.pop(fake_job.id, None)
        # Tests run sequentially so leakage is unlikely but be tidy.
        _ = _asyncio  # silence unused-import lint


def test_fragment_includes_stale_count_oob(client: TestClient) -> None:
    _forge_session(client)
    r = client.get("/chats/fragment?type=all&recency=&folder=")
    assert r.status_code == 200
    # One chat is stale (id=1002).
    assert '<span id="stale-count" hx-swap-oob="true">1</span>' in r.text


# ---------- POST /chats/{id}/sync ----------


def test_sync_one_chat_submits_job(client: TestClient) -> None:
    _forge_session(client)
    r = client.post("/chats/1002/sync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "job_id" in body
    assert body["status"] in (
        "pending", "running", "exporting", "downloading", "deduping",
    )


def test_sync_one_chat_404_for_unknown(client: TestClient) -> None:
    _forge_session(client)
    r = client.post("/chats/99999/sync")
    assert r.status_code == 404
    assert "not found" in r.text.lower() or "не найден" in r.text.lower()


def test_sync_one_chat_unauth_redirects_to_login(client: TestClient) -> None:
    # No session cookie — middleware sends us to /login.
    r = client.post("/chats/1001/sync", follow_redirects=False)
    assert r.status_code in (303, 307)


# ---------- POST /chats/sync-stale ----------


def test_sync_stale_submits_one_job_per_stale_chat(client: TestClient) -> None:
    """Each stale chat gets its own job so the user can see per-chat
    progress and titles in /jobs instead of a single mega-row."""
    _forge_session(client)
    r = client.post("/chats/sync-stale")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "job_ids" in body
    assert isinstance(body["job_ids"], list)
    assert len(body["job_ids"]) == 1  # only chat 1002 is stale
    assert body["chat_ids"] == [1002]
    assert body["count"] == 1
    assert "failures" not in body  # all submissions succeeded


def test_sync_stale_returns_204_when_nothing_stale(
    client: TestClient, tmp_path: Path,
) -> None:
    _forge_session(client)
    # Drain the stale chat by bumping its last_export_at past
    # last_message_date.
    import asyncio

    async def drain() -> None:
        eng = create_engine(tmp_path / "data" / "state.db", dek=None)
        f = create_session_factory(eng)
        from sqlalchemy import update

        async with f() as s:
            await s.execute(
                update(ChatState)
                .where(ChatState.chat_id == 1002)
                .values(last_export_at=datetime.now(UTC) + timedelta(days=1))
            )
            await s.commit()
        await eng.dispose()

    asyncio.run(drain())

    r = client.post("/chats/sync-stale")
    assert r.status_code == 204
