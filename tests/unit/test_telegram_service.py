"""Round-trip tests for ``src.api.telegram_service``.

Uses an in-memory SQLite engine + session factory so we don't touch disk.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from src.api.telegram_service import (
    load_api_credentials,
    load_phone,
    save_api_credentials,
    save_phone,
)
from src.db.session import create_engine, create_schema, create_session_factory


@pytest.fixture
async def factory(tmp_path: Path):
    engine = create_engine(tmp_path / "state.db", dek=None)
    await create_schema(engine)
    f = create_session_factory(engine)
    try:
        yield f
    finally:
        await engine.dispose()


async def test_api_credentials_roundtrip(factory) -> None:
    dek = secrets.token_bytes(32)
    await save_api_credentials(factory, dek, 12345, "deadbeefdeadbeefdeadbeefdeadbeef")

    loaded = await load_api_credentials(factory, dek)
    assert loaded == (12345, "deadbeefdeadbeefdeadbeefdeadbeef")


async def test_api_credentials_overwrite(factory) -> None:
    dek = secrets.token_bytes(32)
    await save_api_credentials(factory, dek, 1, "a" * 32)
    await save_api_credentials(factory, dek, 2, "b" * 32)

    loaded = await load_api_credentials(factory, dek)
    assert loaded == (2, "b" * 32)


async def test_api_credentials_wrong_dek_fails(factory) -> None:
    dek = secrets.token_bytes(32)
    other = secrets.token_bytes(32)
    await save_api_credentials(factory, dek, 9999, "hashhash" * 4)

    with pytest.raises(InvalidTag):
        await load_api_credentials(factory, other)


async def test_load_when_empty(factory) -> None:
    dek = secrets.token_bytes(32)
    assert await load_api_credentials(factory, dek) is None
    assert await load_phone(factory, dek) is None


async def test_phone_roundtrip(factory) -> None:
    dek = secrets.token_bytes(32)
    await save_phone(factory, dek, "+491701234567")
    assert await load_phone(factory, dek) == "+491701234567"


async def test_phone_wrong_dek_fails(factory) -> None:
    dek = secrets.token_bytes(32)
    await save_phone(factory, dek, "+491701234567")
    with pytest.raises(InvalidTag):
        await load_phone(factory, secrets.token_bytes(32))


async def test_phone_and_credentials_coexist(factory) -> None:
    dek = secrets.token_bytes(32)
    await save_api_credentials(factory, dek, 42, "hash" * 8)
    await save_phone(factory, dek, "+491701234567")

    assert await load_api_credentials(factory, dek) == (42, "hash" * 8)
    assert await load_phone(factory, dek) == "+491701234567"
