"""Search across title / username / first_name / last_name in /chats."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.api.routes_chats import _load_chats_with_state
from src.db.models import Chat, ChatType
from src.db.session import create_engine, create_schema, create_session_factory


@pytest.fixture
async def factory(tmp_path: Path):
    eng = create_engine(tmp_path / "state.db", dek=None)
    await create_schema(eng)
    f = create_session_factory(eng)
    async with f() as s:
        # Realistic mix: title-only channel, user with first/last,
        # bot with @username only.
        s.add(Chat(id=1, title="Русская экономика", username="banki_economy",
                   type=ChatType.CHANNEL, is_public=True))
        s.add(Chat(id=2, title="Илья Шмаков", username="ilyshka1",
                   type=ChatType.PRIVATE, is_public=True,
                   first_name="Илья", last_name="Шмаков"))
        s.add(Chat(id=3, title="TON Dating", username="ton_dating_bot",
                   type=ChatType.BOT, is_public=True))
        await s.commit()
    yield f
    await eng.dispose()


async def test_search_by_last_name_cyrillic(factory) -> None:
    rows = await _load_chats_with_state(factory, query="Шмаков")
    assert len(rows) == 1 and rows[0]["id"] == 2


async def test_search_by_first_name_lower(factory) -> None:
    rows = await _load_chats_with_state(factory, query="илья")
    assert len(rows) == 1 and rows[0]["id"] == 2


async def test_search_by_partial_username(factory) -> None:
    rows = await _load_chats_with_state(factory, query="ilyshka")
    assert len(rows) == 1 and rows[0]["id"] == 2


async def test_search_by_partial_title(factory) -> None:
    rows = await _load_chats_with_state(factory, query="экономика")
    assert len(rows) == 1 and rows[0]["id"] == 1


async def test_empty_query_returns_all(factory) -> None:
    rows = await _load_chats_with_state(factory, query="")
    assert len(rows) == 3


async def test_search_misses(factory) -> None:
    rows = await _load_chats_with_state(factory, query="никто_не_совпадёт")
    assert rows == []
