"""Async SQLAlchemy engine + session factory.

SQLCipher support is optional. If `sqlcipher3` is importable we build an engine
that passes the DEK as a `PRAGMA key=`; otherwise we fall back to plain
`aiosqlite` (used by tests).  Production boots will fail loudly when a DEK is
provided and SQLCipher is not present.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.db.models import Base


class SessionFactory(Protocol):
    def __call__(self) -> AsyncSession: ...


def _hex_key(dek: bytes) -> str:
    """Format a raw DEK for SQLCipher's `PRAGMA key = "x'...'"`."""
    return f"x'{dek.hex()}'"


# Default ``busy_timeout`` for SQLite connections, in milliseconds.
# Bumped from sqlite's hard-coded 0 (= immediate ``database is locked``)
# so a connection waiting on another writer politely sleeps + retries
# for up to 30 s before erroring out. The Deduplicator's own
# asyncio.Lock keeps the in-process contention near zero; this PRAGMA
# is the safety net for cross-component races (e.g. JobPersistence
# writing job snapshots while ChatState advance for another chat
# commits).
_SQLITE_BUSY_TIMEOUT_MS = 30_000


def _apply_sqlite_pragmas(dbapi_conn) -> None:  # noqa: ANN001 — sqlite/sqlcipher3 conn
    """Per-connection PRAGMAs that improve concurrency safety.

    * ``journal_mode=WAL`` allows readers to proceed while a writer is
      committing — without it the default rollback journal blocks every
      reader during a write, which on this app means /chats stalls
      whenever a dedup INSERT is in flight.
    * ``busy_timeout`` makes contending writers wait instead of
      immediately raising ``database is locked``. Combined with the
      Deduplicator's own asyncio.Lock this all but eliminates the
      contention class the user reported during bulk-sync runs.
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    finally:
        cur.close()


def create_engine(
    db_path: Path,
    *,
    dek: bytes | None = None,
    echo: bool = False,
) -> AsyncEngine:
    """Build an async engine for the state DB.

    - `dek=None` → plain `aiosqlite` (used by tests and the first-run setup
      where we don't yet have a key).
    - `dek=bytes` → SQLCipher via `sqlcipher3`. The driver wires PRAGMA key at
      connect time.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if dek is None:
        from sqlalchemy import event

        url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
        engine = create_async_engine(url, echo=echo, future=True)

        @event.listens_for(engine.sync_engine, "connect")
        def _set_pragmas_plain(dbapi_conn, _conn_record):  # type: ignore[no-untyped-def]
            _apply_sqlite_pragmas(dbapi_conn)

        return engine

    try:
        import sqlcipher3  # noqa: F401
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "SQLCipher requested (DEK provided) but `sqlcipher3` is not installed. "
            "Install `sqlcipher3-binary` or run with DEK=None for plain sqlite."
        ) from exc

    # The async aiosqlite driver does not carry sqlcipher3. We inject a
    # custom `creator` that opens a sqlcipher3 connection and sets PRAGMA key.
    from sqlalchemy import event
    from sqlalchemy.pool import StaticPool

    hex_key = _hex_key(dek)
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    engine = create_async_engine(
        url,
        echo=echo,
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlcipher_key(dbapi_conn, _conn_record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute(f'PRAGMA key = "{hex_key}"')
        cur.execute("PRAGMA cipher_page_size = 4096")
        cur.execute("PRAGMA kdf_iter = 256000")
        cur.close()
        # WAL + busy_timeout apply to encrypted DBs too — same
        # contention class, same fix. Must run AFTER ``PRAGMA key``
        # because every PRAGMA before unlocking would itself error.
        _apply_sqlite_pragmas(dbapi_conn)

    return engine


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# Lightweight, Alembic-free column migrations. ``create_all`` only creates
# *missing tables* — it never ALTERs a table that already exists, so a new
# column added to an already-shipped table is silently absent on upgraded
# installs. We add such columns by hand here, guarded by a PRAGMA check so the
# statement is skipped (not retried-into-an-error) when the column is present.
# Each entry is (table, column, column_ddl). SQLite ALTER TABLE ADD COLUMN
# accepts a NOT NULL column only when a DEFAULT is supplied — hence the
# ``DEFAULT 0`` on watch_enabled.
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("chat_states", "export_cursor_message_id", "BIGINT"),
    ("chat_states", "watch_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("chat_states", "watch_interval_seconds", "INTEGER"),
    ("chat_states", "watch_last_checked_at", "DATETIME"),
)


def _apply_column_migrations(sync_conn) -> None:  # noqa: ANN001 — sync DBAPI conn
    """Add any missing columns listed in ``_COLUMN_MIGRATIONS``.

    Runs inside ``run_sync`` so we can use the plain DBAPI cursor for
    ``PRAGMA table_info`` (the cross-dialect way to introspect columns on
    both plain SQLite and SQLCipher).
    """
    from sqlalchemy import text

    for table, column, ddl in _COLUMN_MIGRATIONS:
        existing = {
            row[1] for row in sync_conn.execute(text(f"PRAGMA table_info({table})"))
        }
        if column not in existing:
            sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def create_schema(engine: AsyncEngine) -> None:
    """Create all tables if they don't exist, then apply column migrations.

    Idempotent: safe to call on every boot.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_column_migrations)


@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Short context manager: begin, commit on success, rollback on error."""
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
