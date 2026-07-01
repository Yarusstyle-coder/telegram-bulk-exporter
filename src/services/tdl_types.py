"""Pydantic models for the tdl subprocess wrapper.

These are the structured events and results emitted by
:mod:`src.services.tdl_wrapper` and parsed by
:mod:`src.services.tdl_progress_parser`.

The models are intentionally minimal — enough to drive a UI progress bar,
surface actionable errors (flood wait, auth), and record a summary of what
was exported/downloaded. They are frozen where the value is logically
immutable so callers can rely on identity semantics when pushing them
through queues.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class TdlProgress(BaseModel):
    """A single progress frame parsed from tdl stdout/stderr."""

    model_config = ConfigDict(frozen=True)

    stage: Literal["export", "download"]
    current: int
    total: int
    percent: float
    speed_bps: int | None = None
    eta_seconds: int | None = None
    file: str | None = None


class TdlError(BaseModel):
    """A structured error extracted from tdl output."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["flood_wait", "network", "auth", "unknown"]
    message: str
    wait_seconds: int | None = None


class ChatExportResult(BaseModel):
    """Summary of a `tdl chat export` invocation."""

    model_config = ConfigDict(frozen=True)

    chat_id: int
    count_messages: int
    first_id: int | None
    last_id: int | None
    path: Path
    raw_json_meta: dict


class DlResult(BaseModel):
    """Summary of a `tdl dl` invocation."""

    model_config = ConfigDict(frozen=True)

    files_downloaded: int
    bytes_total: int
    elapsed_seconds: float
    errors: list[TdlError] = []
