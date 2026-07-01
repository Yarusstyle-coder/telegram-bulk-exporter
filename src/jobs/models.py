"""Pydantic v2 models for job settings and progress updates.

These models live independently of the SQLAlchemy layer in `src/db/models.py`:
- Job *settings* are a per-job snapshot of UI toggles (serialised as JSON into
  `ExportJob.settings_json`).
- Job *updates* travel on the WebSocket stream; they are fire-and-forget.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MediaType(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    VOICE = "voice"          # voice notes
    VIDEO_NOTE = "video_note"
    AUDIO = "audio"          # music files
    DOCUMENT = "document"
    STICKER = "sticker"
    GIF = "gif"


DEFAULT_MEDIA_TYPES: list[MediaType] = [
    MediaType.PHOTO,
    MediaType.VIDEO,
    MediaType.VOICE,
    MediaType.VIDEO_NOTE,
    MediaType.AUDIO,
    MediaType.DOCUMENT,
    MediaType.STICKER,
    MediaType.GIF,
]


class JobSettings(BaseModel):
    """UI-specified settings for one export run, per chat."""

    model_config = ConfigDict(extra="forbid")

    chat_ids: list[int] = Field(default_factory=list)
    media_types: list[MediaType] = Field(default_factory=lambda: list(DEFAULT_MEDIA_TYPES))
    max_file_size_bytes: int | None = Field(default=None, ge=0)
    only_new: bool = True
    dedup: bool = True
    with_content: bool = True
    threads_per_file: int = Field(default=4, ge=1, le=16)
    parallel_tasks: int = Field(default=2, ge=1, le=8)
    output_dir: str | None = None  # relative to settings.export_dir
    # Date-window filter for messages. Either bound is optional.
    # ISO-8601 string ("2025-01-01") in the UI; parsed into a UTC datetime
    # in the exporter's _filter_manifest.
    date_from: str | None = None
    date_to: str | None = None
    # Cap the export to the last N messages (server-side via tdl `-T last`).
    # Useful for proof-of-concept runs on huge channels.
    recent_messages: int | None = Field(default=None, ge=1, le=100000)


class JobUpdateKind(str, Enum):
    STATUS = "status"
    PROGRESS = "progress"
    LOG = "log"
    ERROR = "error"
    COMPLETE = "complete"


class JobUpdate(BaseModel):
    """One event pushed to the live-stream WebSocket."""

    model_config = ConfigDict(extra="forbid")

    kind: JobUpdateKind
    ts: datetime
    job_id: str
    chat_id: int | None = None

    # Fields are a union of what each kind needs — the client branches on `kind`.
    status: str | None = None
    percent: float | None = Field(default=None, ge=0, le=100)
    current: int | None = None
    total: int | None = None
    file: str | None = None
    speed_bps: int | None = None
    eta_seconds: int | None = None
    message: str | None = None
    level: Literal["info", "warn", "error"] | None = None
    bytes_saved: int | None = None
    files_deduped: int | None = None
