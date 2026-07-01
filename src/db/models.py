"""SQLAlchemy 2.0 models.

All persisted tables live behind this module. Production deployment uses
SQLCipher to encrypt the whole DB file; tests may run against plain SQLite
via `aiosqlite` — the `session.create_engine()` helper picks the right driver.

Design notes
------------
- `Chat`: cached mirror of a Telegram dialog. Refreshed on demand.
- `ChatState`: per-chat incremental-export cursor. `last_exported_message_id`
  is the watermark; new exports start at `last + 1`.
- `MediaFile`: the dedup index. Key is SHA-256; `original_path` is the
  canonical on-disk location for content addressed by that hash.
- `ExportJob`: historical record of export runs (one row per UI job).
- `UserSecret`: single-row table holding the encrypted TOTP secret,
  encrypted api_hash, and backup-code hashes. Each blob is AES-GCM-encrypted
  with the session DEK *before* insertion — SQLCipher is the outer layer,
  these blobs are the inner layer.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ChatType(str, enum.Enum):
    PRIVATE = "private"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"
    BOT = "bot"
    UNKNOWN = "unknown"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    EXPORTING = "exporting"
    DOWNLOADING = "downloading"
    DEDUPING = "deduping"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    type: Mapped[ChatType] = mapped_column(Enum(ChatType), default=ChatType.UNKNOWN, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(256))
    last_name: Mapped[str | None] = mapped_column(String(256))
    approx_message_count: Mapped[int | None] = mapped_column(Integer)
    avatar_path: Mapped[str | None] = mapped_column(String(512))
    last_message_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # JSON-encoded list of Telegram folder ids the chat belongs to.
    folder_ids_json: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    state: Mapped[ChatState | None] = relationship(
        back_populates="chat", uselist=False, cascade="all, delete-orphan"
    )
    export_jobs: Mapped[list[ExportJob]] = relationship(back_populates="chat")


class ChatState(Base):
    __tablename__ = "chat_states"

    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    last_exported_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_export_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- id-range chunking (resumable large-chat export) ---
    # Durable cursor for an IN-PROGRESS chunked export sweep. Distinct from
    # ``last_exported_message_id`` (the committed watermark of the last fully
    # completed export): the cursor records the top of the last successfully
    # exported+downloaded window so a crash mid-sweep resumes from cursor+1
    # instead of re-walking from the watermark. NULL ⇒ no sweep in progress.
    export_cursor_message_id: Mapped[int | None] = mapped_column(BigInteger)

    # --- per-chat auto-update (watch for new messages, sync automatically) ---
    # Opt-in flag set by the "Авто" toggle in the chat list.
    watch_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    # Per-chat override for the poll interval, in seconds. NULL ⇒ use the
    # global default (see user_prefs ``default_watch_interval_seconds``).
    watch_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    # When the auto-update scheduler last enqueued (or considered) a sync for
    # this chat — lets the poller honour the interval without re-querying.
    watch_last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    chat: Mapped[Chat] = relationship(back_populates="state")


class MediaFile(Base):
    """Dedup index: SHA-256 → canonical file location."""

    __tablename__ = "media_files"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    original_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    link_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    bytes_saved_via_links: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("original_path", name="uq_media_path"),)


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.PENDING, nullable=False
    )
    settings_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON snapshot of UI settings

    from_message_id: Mapped[int | None] = mapped_column(BigInteger)
    to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    resumed_from_job_id: Mapped[str | None] = mapped_column(String(36))

    bytes_downloaded: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    files_downloaded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    files_deduped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bytes_saved: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    error_message: Mapped[str | None] = mapped_column(Text)
    log_path: Mapped[str | None] = mapped_column(String(1024))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    chat: Mapped[Chat] = relationship(back_populates="export_jobs")


class DialogFolderRow(Base):
    """Mirror of a Telegram folder (DialogFilter)."""

    __tablename__ = "dialog_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    chat_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class UserSecret(Base):
    """Singleton row (id=1) holding AES-GCM-encrypted app secrets.

    Each blob is wrapped `{nonce(12) | ciphertext | tag(16)}` with the session
    DEK. Outer SQLCipher layer encrypts the whole DB page — blobs are double-
    wrapped so that even a DB dump is useless without the DEK.
    """

    __tablename__ = "user_secrets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enc_api_id: Mapped[bytes | None] = mapped_column(LargeBinary)
    enc_api_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    enc_phone: Mapped[bytes | None] = mapped_column(LargeBinary)
    enc_totp_secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    enc_backup_codes_json: Mapped[bytes | None] = mapped_column(LargeBinary)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
