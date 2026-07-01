"""Tiny helper: persist & humanise the "last chats refresh" timestamp.

Extracted from `routes_chats.py` to keep that module a reasonable size.
The marker is a plain ISO-8601 UTC string in
``data_dir/last_chats_refresh.txt`` — small enough that the I/O hit is
negligible, but it survives server restarts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.config import get_settings
from src.logging_setup import get_logger

log = get_logger(__name__)

REFRESH_STAMP_FILE = "last_chats_refresh.txt"


def refresh_stamp_path() -> Path:
    """Resolve the absolute path of the marker file under settings.data_dir."""
    s = get_settings()
    try:
        base = Path(s.data_dir)
    except Exception:  # noqa: BLE001 — defensive: derive from db_path instead
        base = Path(s.db_path).parent
    return base / REFRESH_STAMP_FILE


def write_refresh_stamp(now: datetime | None = None) -> str:
    """Persist the current UTC time as ISO-8601 to the marker file.

    Returns the ISO string that was written (even if the I/O failed; the
    caller can still surface it to the response). Errors are logged at
    debug-level and swallowed."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    iso = moment.isoformat()
    try:
        path = refresh_stamp_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(iso, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.debug("refresh_stamp_write_failed", error=str(exc))
    return iso


def read_refresh_stamp() -> datetime | None:
    """Load the last-refresh timestamp, or None if missing/unreadable."""
    try:
        raw = refresh_stamp_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        log.debug("refresh_stamp_read_failed", error=str(exc))
        return None
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def humanise_refreshed(
    dt: datetime | None, now: datetime | None = None
) -> str | None:
    """Return a Russian relative phrase like '3 мин назад' / 'вчера'.

    Returns None when *dt* is None so the template can hide the label."""
    if dt is None:
        return None
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    secs = int((moment - dt).total_seconds())
    if secs < 60:
        return "только что"
    mins = secs // 60
    if mins < 60:
        return f"{mins} мин назад"
    hours = mins // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} дн назад"
    return dt.strftime("%Y-%m-%d")
