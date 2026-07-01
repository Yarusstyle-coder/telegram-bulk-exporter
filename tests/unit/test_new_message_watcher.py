"""Tests for the Telethon NewMessage watcher that mirrors a fresh
message's timestamp into ``chats.last_message_date`` so /chats can
flip the "stale" badge in real-time — without waiting for the user
to click "Обновить список".

We don't spin up a real Telethon client here: we hand
``TelegramSessionManager._register_new_message_handler`` a fake
client whose ``on(...)`` decorator just captures the registered
callable. Then we synthesise a NewMessage event and assert the DB
row gets the new ``last_message_date``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from src.db.models import Chat, ChatType
from src.db.session import (
    create_engine,
    create_schema,
    create_session_factory,
)
from src.telegram.telethon_client import TelegramSessionManager


class _FakeClient:
    """Bare-bones stand-in for telethon.TelegramClient.

    Captures whatever callable the manager registers via ``@client.on(...)``
    so the test can invoke it on demand with a synthesised event.
    """

    def __init__(self) -> None:
        self.handlers: list = []
        # Mirrors the attribute the manager uses to detect dup-registration.
        self._tge_new_message_handler = False

    def on(self, _event_filter):  # noqa: ANN001
        def _wrap(fn):
            self.handlers.append(fn)
            return fn

        return _wrap


class _FakeMessage:
    def __init__(self, dt: datetime) -> None:
        self.date = dt


class _FakeEvent:
    def __init__(self, chat_id: int, dt: datetime) -> None:
        self.chat_id = chat_id
        self.message = _FakeMessage(dt)


def _build_manager() -> TelegramSessionManager:
    """Skip the constructor's heavy lifting — we only need an instance
    with ``_register_new_message_handler`` callable."""
    return TelegramSessionManager(
        api_id=1,
        api_hash="hash",
        session_path=Path("/tmp/never-used.tgsess"),
        dek=None,
        proxy=None,
    )


@pytest.fixture
def factory(tmp_path: Path):  # noqa: ANN201
    """Tmp-DB session factory pre-seeded with one chat row."""
    async def _setup():
        eng = create_engine(tmp_path / "state.db", dek=None)
        await create_schema(eng)
        f = create_session_factory(eng)
        async with f() as s:
            s.add(Chat(
                id=4242, title="Светлана Медик кемерово", type=ChatType.PRIVATE,
                last_message_date=datetime.now(UTC) - timedelta(days=3),
            ))
            await s.commit()
        return f, eng

    f, eng = asyncio.run(_setup())
    yield f
    asyncio.run(eng.dispose())


def test_handler_updates_last_message_date(factory) -> None:  # noqa: ANN001
    mgr = _build_manager()
    fake_client = _FakeClient()
    mgr._register_new_message_handler(fake_client, factory)
    # Exactly one handler should have been registered.
    assert len(fake_client.handlers) == 1
    handler = fake_client.handlers[0]
    # Fire a fresh event from chat 4242.
    fresh = datetime(2026, 5, 18, 12, 30, tzinfo=UTC)
    asyncio.run(handler(_FakeEvent(4242, fresh)))
    # Reread the chat row — last_message_date should now match.
    async def _read():
        async with factory() as s:
            row = (
                await s.execute(select(Chat).where(Chat.id == 4242))
            ).scalar_one()
        return row.last_message_date

    result = asyncio.run(_read())
    # Telethon emits tz-aware datetimes; SQLAlchemy DateTime(tz=True)
    # round-trips them as naïve on SQLite. Compare on epoch seconds so
    # the test doesn't break on tzinfo stripping.
    expected_ts = fresh.timestamp()
    actual_ts = (
        result.replace(tzinfo=UTC) if result.tzinfo is None else result
    ).timestamp()
    assert abs(actual_ts - expected_ts) < 1.0


def test_handler_registration_is_idempotent(factory) -> None:  # noqa: ANN001
    """Re-attaching on the same client object must not stack duplicate
    handlers — that would cause N writes per message after N reconnects.
    """
    mgr = _build_manager()
    fake_client = _FakeClient()
    mgr._register_new_message_handler(fake_client, factory)
    mgr._register_new_message_handler(fake_client, factory)
    mgr._register_new_message_handler(fake_client, factory)
    assert len(fake_client.handlers) == 1


def test_handler_ignores_event_without_date(factory) -> None:  # noqa: ANN001
    """A malformed event (no message / no date) must be silently
    skipped — Telethon occasionally fires service-only events without
    a usable timestamp, and crashing the event loop on those would
    silently stop ALL future updates."""
    mgr = _build_manager()
    fake_client = _FakeClient()
    mgr._register_new_message_handler(fake_client, factory)
    handler = fake_client.handlers[0]

    class _NoDate:
        chat_id = 4242
        message = None

    # Must not raise.
    asyncio.run(handler(_NoDate()))

    async def _read():
        async with factory() as s:
            row = (
                await s.execute(select(Chat).where(Chat.id == 4242))
            ).scalar_one()
        return row.last_message_date

    # Original seeded value should remain untouched.
    original = asyncio.run(_read())
    assert original is not None
