"""Unit tests for ``AutoUpdateScheduler._tick`` — the per-chat poll pass.

These drive ``_tick()`` directly against a real temp SQLite DB and a fake
``JobManager``, with ``now_fn`` injected for deterministic time. They pin
the staleness rule, the per-chat interval gate, the active-job dedup, the
``watch_enabled`` opt-in, and the tdl-login veto.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.db.models import Chat, ChatState, ChatType
from src.db.session import create_engine, create_schema, create_session_factory
from src.jobs.models import JobSettings
from src.services.auto_update import AutoUpdateScheduler

# Fixed "now" for every case — keeps interval / staleness arithmetic exact.
NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fakes / fixtures
# --------------------------------------------------------------------------


class _Status:
    """Stand-in for ``JobStatus`` exposing only ``.value`` (all the scheduler
    reads)."""

    def __init__(self, value: str) -> None:
        self.value = value


class _StubJob:
    """Minimal Job-like object for seeding ``list_jobs()``."""

    def __init__(self, *, status: str, chat_ids: list[int]) -> None:
        self.status = _Status(status)
        self.settings = JobSettings(chat_ids=chat_ids)


class FakeJobManager:
    """Records ``submit`` calls; returns a configurable ``list_jobs``."""

    def __init__(self, existing: list[Any] | None = None) -> None:
        self._existing = list(existing or [])
        self.submitted: list[JobSettings] = []

    def list_jobs(self) -> list[Any]:
        return list(self._existing)

    async def submit(self, settings: JobSettings) -> _StubJob:
        self.submitted.append(settings)
        # The scheduler only scans ``list_jobs()`` for dedup, never its own
        # returned job, so a bare stub is sufficient.
        return _StubJob(status="pending", chat_ids=list(settings.chat_ids))


@pytest.fixture
async def session_factory(tmp_path: Path):
    """A real (plain-SQLite) session factory over a fresh schema."""
    engine = create_engine(tmp_path / "state.db", dek=None)
    await create_schema(engine)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _seed(
    factory: Any,
    *,
    chat_id: int = 1001,
    watch_enabled: bool = True,
    last_exported_message_id: int | None = 10,
    last_export_at: datetime | None = None,
    last_message_date: datetime | None = None,
    watch_interval_seconds: int | None = None,
    watch_last_checked_at: datetime | None = None,
) -> None:
    """Insert one Chat + ChatState row with the given watch parameters."""
    if last_export_at is None:
        last_export_at = NOW - timedelta(days=1)
    if last_message_date is None:
        last_message_date = NOW  # fresher than last_export_at → stale by default
    async with factory() as s:
        s.add(
            Chat(
                id=chat_id,
                title=f"Chat {chat_id}",
                type=ChatType.PRIVATE,
                last_message_date=last_message_date,
            )
        )
        s.add(
            ChatState(
                chat_id=chat_id,
                last_exported_message_id=last_exported_message_id,
                last_export_at=last_export_at,
                watch_enabled=watch_enabled,
                watch_interval_seconds=watch_interval_seconds,
                watch_last_checked_at=watch_last_checked_at,
            )
        )
        await s.commit()


async def _read_last_checked(factory: Any, chat_id: int) -> datetime | None:
    from sqlalchemy import select

    async with factory() as s:
        st = (
            await s.execute(
                select(ChatState).where(ChatState.chat_id == chat_id)
            )
        ).scalar_one()
        return st.watch_last_checked_at


def _make(factory: Any, jm: FakeJobManager, **kwargs: Any) -> AutoUpdateScheduler:
    return AutoUpdateScheduler(
        session_factory=factory,
        job_manager=jm,
        now_fn=lambda: NOW,
        **kwargs,
    )


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------


async def test_stale_watched_chat_submits_one_only_new_job(session_factory) -> None:
    """Case 1: watch_enabled + stale + no prior check → one only_new job;
    watch_last_checked_at stamped in the DB."""
    await _seed(session_factory, chat_id=1001, watch_last_checked_at=None)
    jm = FakeJobManager()
    scheduler = _make(session_factory, jm)

    submitted = await scheduler._tick()

    assert submitted == 1
    assert len(jm.submitted) == 1
    settings = jm.submitted[0]
    assert settings.chat_ids == [1001]
    assert settings.only_new is True
    assert settings.dedup is True
    # Stamp landed and equals injected NOW (compared tz-aware after coerce).
    stamped = await _read_last_checked(session_factory, 1001)
    assert stamped is not None
    coerced = stamped if stamped.tzinfo is not None else stamped.replace(tzinfo=UTC)
    assert coerced == NOW


async def test_not_stale_chat_is_skipped(session_factory) -> None:
    """Case 2: last_message_date <= last_export_at → not stale → 0 submits."""
    await _seed(
        session_factory,
        chat_id=1001,
        last_export_at=NOW,  # export AFTER (or equal to) the last message
        last_message_date=NOW - timedelta(hours=1),
    )
    jm = FakeJobManager()
    scheduler = _make(session_factory, jm)

    submitted = await scheduler._tick()

    assert submitted == 0
    assert jm.submitted == []


async def test_recently_checked_chat_is_skipped(session_factory) -> None:
    """Case 3: stale but checked 5 min ago with a 3600 s interval → 0
    submits (interval gate)."""
    await _seed(
        session_factory,
        chat_id=1001,
        watch_interval_seconds=None,  # falls back to default 3600
        watch_last_checked_at=NOW - timedelta(minutes=5),
    )
    jm = FakeJobManager()
    scheduler = _make(session_factory, jm, default_interval_seconds=3600)

    submitted = await scheduler._tick()

    assert submitted == 0
    assert jm.submitted == []


async def test_active_job_dedups_submission(session_factory) -> None:
    """Case 4: stale + an active job already exists for the chat → 0
    submits (dedup against list_jobs())."""
    await _seed(session_factory, chat_id=1001)
    jm = FakeJobManager(
        existing=[_StubJob(status="downloading", chat_ids=[1001])]
    )
    scheduler = _make(session_factory, jm)

    submitted = await scheduler._tick()

    assert submitted == 0
    assert jm.submitted == []


async def test_watch_disabled_chat_is_skipped(session_factory) -> None:
    """Case 5: watch_enabled False → 0 submits even when stale."""
    await _seed(session_factory, chat_id=1001, watch_enabled=False)
    jm = FakeJobManager()
    scheduler = _make(session_factory, jm)

    submitted = await scheduler._tick()

    assert submitted == 0
    assert jm.submitted == []


async def test_tdl_login_check_false_vetoes_pass(session_factory) -> None:
    """Case 6: tdl_login_check returns False → whole pass vetoed, 0
    submits."""
    await _seed(session_factory, chat_id=1001)
    jm = FakeJobManager()
    scheduler = _make(session_factory, jm, tdl_login_check=lambda: False)

    submitted = await scheduler._tick()

    assert submitted == 0
    assert jm.submitted == []


# --------------------------------------------------------------------------
# Extra coverage — async login check + interval override actually firing
# --------------------------------------------------------------------------


async def test_async_tdl_login_check_true_allows_submit(session_factory) -> None:
    """An awaitable tdl_login_check that resolves truthy still submits."""
    await _seed(session_factory, chat_id=1001)
    jm = FakeJobManager()

    async def _logged_in() -> bool:
        return True

    scheduler = _make(session_factory, jm, tdl_login_check=_logged_in)

    submitted = await scheduler._tick()

    assert submitted == 1
    assert jm.submitted[0].chat_ids == [1001]


async def test_per_chat_interval_override_allows_submit(session_factory) -> None:
    """A short per-chat ``watch_interval_seconds`` lets a chat checked 5 min
    ago submit again (60 s interval < 300 s gap)."""
    await _seed(
        session_factory,
        chat_id=1001,
        watch_interval_seconds=60,
        watch_last_checked_at=NOW - timedelta(minutes=5),
    )
    jm = FakeJobManager()
    scheduler = _make(session_factory, jm, default_interval_seconds=3600)

    submitted = await scheduler._tick()

    assert submitted == 1
    assert jm.submitted[0].chat_ids == [1001]
