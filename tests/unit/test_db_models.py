"""Unit tests for the DB layer: schema creates, models CRUD, JSON encoding."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from src.db.models import Chat, ChatState, ChatType, ExportJob, JobStatus, MediaFile, UserSecret
from src.db.session import create_engine, create_schema, create_session_factory


@pytest.fixture
async def engine(tmp_path: Path):
    eng = create_engine(tmp_path / "state.db", dek=None)
    await create_schema(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    factory = create_session_factory(engine)
    async with factory() as s:
        yield s


async def test_schema_creates_all_tables(engine) -> None:
    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {r[0] for r in rows.fetchall()}
    expected = {"chats", "chat_states", "media_files", "export_jobs", "user_secrets"}
    assert expected.issubset(names)


async def test_chat_and_state_roundtrip(session) -> None:
    c = Chat(id=1001, title="friend", type=ChatType.PRIVATE, approx_message_count=500)
    c.state = ChatState(chat_id=1001, last_exported_message_id=42, total_size_bytes=1024, total_files=1)
    session.add(c)
    await session.commit()

    loaded = (await session.execute(select(Chat).where(Chat.id == 1001))).scalar_one()
    assert loaded.title == "friend"
    assert loaded.type is ChatType.PRIVATE
    assert loaded.state is not None
    assert loaded.state.last_exported_message_id == 42


async def test_export_job_with_settings_json(session) -> None:
    c = Chat(id=2002, title="group chat", type=ChatType.GROUP)
    session.add(c)
    await session.flush()

    job = ExportJob(
        id=str(uuid.uuid4()),
        chat_id=2002,
        status=JobStatus.PENDING,
        settings_json=json.dumps({"media_types": ["photo", "video"], "only_new": True}),
    )
    session.add(job)
    await session.commit()

    loaded = (await session.execute(select(ExportJob).where(ExportJob.chat_id == 2002))).scalar_one()
    s = json.loads(loaded.settings_json)
    assert s["only_new"] is True
    assert loaded.status is JobStatus.PENDING


async def test_media_file_unique_path(session) -> None:
    a = MediaFile(sha256="a" * 64, original_path="/tmp/a.jpg", size_bytes=1024)
    b = MediaFile(sha256="b" * 64, original_path="/tmp/a.jpg", size_bytes=1024)  # same path, different hash
    session.add(a)
    await session.commit()

    session.add(b)
    with pytest.raises(Exception):  # IntegrityError wrapped
        await session.commit()


async def test_user_secret_blob_storage(session) -> None:
    # Simulate: nonce(12) + ciphertext + tag(16)
    blob = b"\x00" * 12 + b"encrypted_api_hash_ciphertext" + b"\x01" * 16
    u = UserSecret(id=1, enc_api_hash=blob, enc_totp_secret=b"\x02" * 60)
    session.add(u)
    await session.commit()

    loaded = (await session.execute(select(UserSecret).where(UserSecret.id == 1))).scalar_one()
    assert loaded.enc_api_hash == blob
    assert loaded.enc_totp_secret == b"\x02" * 60


async def test_datetimes_default_utc(session) -> None:
    c = Chat(id=3003, title="stamp", type=ChatType.CHANNEL)
    session.add(c)
    await session.commit()
    assert c.created_at.tzinfo is not None
    # It should be close to "now":
    delta = datetime.now(UTC) - c.created_at
    assert delta.total_seconds() < 60


async def test_chat_type_enum_values() -> None:
    # Sanity: enum serializes to its string value
    assert ChatType.PRIVATE.value == "private"
    assert JobStatus.SUCCEEDED.value == "succeeded"


async def test_connection_uses_wal_and_busy_timeout(engine) -> None:
    """Every connection from our engine MUST land in WAL journal mode
    and carry a non-zero ``busy_timeout``. Without the pragmas the
    user hits "database is locked" the moment two writers race —
    which is exactly what bulk-sync triggers in the dedup phase.
    The defaults here are what ``src/db/session.py`` injects via the
    SQLAlchemy ``connect`` event listener.
    """
    async with engine.connect() as conn:
        mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
        timeout = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()
    # SQLite returns the new journal mode lowercased.
    assert str(mode).lower() == "wal"
    # 30 s — matches ``_SQLITE_BUSY_TIMEOUT_MS`` in src/db/session.py.
    assert int(timeout) >= 30_000
