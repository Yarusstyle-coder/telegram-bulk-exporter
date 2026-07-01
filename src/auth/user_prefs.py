"""Lightweight per-user preferences that need to survive process
restarts — currently just the auto-lock duration the user picks on
the 2FA page, but the file format leaves room for more without
schema churn.

Lives at ``<data_dir>/user_prefs.json`` (sibling of ``vault.json``).
Plain text on purpose — the contents are non-sensitive (a lock
interval and a few future settings flags). The file is not part of
the encrypted vault because we need to read it on cold start before
the user has typed their master password.

Schema::

    {
        "auto_lock_seconds": 0,    # 0 = never auto-lock; positive = N seconds
        "schema_version": 1
    }

Anything else in the file is preserved on round-trip, so adding new
fields is backwards-compatible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.config import get_settings
from src.logging_setup import get_logger

log = get_logger(__name__)

_CURRENT_VERSION = 1
# Choices we expose in the UI. Order matters — used for radio render.
# (label, value-in-seconds, slug-for-form-value)
LOCK_DURATION_CHOICES: list[tuple[str, int, str]] = [
    ("Не спрашивать до перезапуска", 0, "until_restart"),
    ("Раз в 12 часов", 12 * 3600, "12h"),
    ("Раз в 4 часа", 4 * 3600, "4h"),
    ("Раз в час", 3600, "1h"),
]


def _path() -> Path:
    return get_settings().data_dir / "user_prefs.json"


def load_user_prefs() -> dict[str, Any]:
    """Return the user prefs blob, or sane defaults when the file is
    missing / corrupt. Never raises — boot must continue even if the
    JSON got truncated during a crash."""
    p = _path()
    if not p.exists() or p.stat().st_size == 0:
        return {"auto_lock_seconds": 0, "schema_version": _CURRENT_VERSION}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("user_prefs root is not an object")
    except (OSError, ValueError) as exc:
        log.warning("user_prefs_load_failed", path=str(p), error=str(exc))
        return {"auto_lock_seconds": 0, "schema_version": _CURRENT_VERSION}
    data.setdefault("auto_lock_seconds", 0)
    data.setdefault("schema_version", _CURRENT_VERSION)
    return data


def save_user_prefs(prefs: dict[str, Any]) -> None:
    """Write the prefs to disk atomically. Best-effort — failures are
    logged but never raised so the request keeps going."""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(prefs)
    payload.setdefault("schema_version", _CURRENT_VERSION)
    try:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("user_prefs_save_failed", path=str(p), error=str(exc))


def slug_to_seconds(slug: str) -> int | None:
    """Map a form-value slug from LOCK_DURATION_CHOICES to seconds.

    Returns None for unknown slugs so callers can fall back to the
    previous value instead of dropping the user back into "never
    locks" by accident.
    """
    for _label, seconds, key in LOCK_DURATION_CHOICES:
        if key == slug:
            return seconds
    return None


def seconds_to_slug(seconds: int) -> str:
    """Reverse map — used by the 2FA template to pre-check the radio
    matching the current setting."""
    for _label, secs, key in LOCK_DURATION_CHOICES:
        if secs == seconds:
            return key
    # Custom value (set via env or future API) — return the "never"
    # slug so the UI doesn't render a dangling-state.
    return "until_restart"
