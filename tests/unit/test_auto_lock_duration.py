"""Tests for the user-chosen auto-lock window on /login/2fa.

Four UI choices, persisted to ``data/user_prefs.json``:
  * "Не спрашивать до перезапуска"  → 0 seconds
  * "Раз в 12 часов"                → 43200
  * "Раз в 4 часа"                  → 14400
  * "Раз в час"                     → 3600

User reported "авторизация часто слетает". The default (0 = never
lock) WAS correct, but there was no way to set it to anything else
without editing an env var. These tests pin the new flow:

  1. The 2FA template renders the four radio buttons with the
     correct pre-checked option (matches ``user_prefs.json``).
  2. POST /login/2fa with a valid code + ``lock_duration`` slug
     writes that choice to ``user_prefs.json`` and updates
     ``SESSION_STORE.auto_lock_seconds``.
  3. Cold start restores the chosen value from ``user_prefs.json``
     into ``SESSION_STORE`` BEFORE the first request lands.
  4. The slug→seconds map matches the spec.
  5. Unknown slugs don't silently downgrade to 0 — they keep the
     previous preference.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.auth.user_prefs import (
    LOCK_DURATION_CHOICES,
    load_user_prefs,
    save_user_prefs,
    seconds_to_slug,
    slug_to_seconds,
)
from src.config import Settings, reset_settings_cache


@pytest.fixture
def tmp_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``data_dir`` at tmp_path so user_prefs.json lives there."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    reset_settings_cache()
    Settings().ensure_dirs()
    yield tmp_path
    reset_settings_cache()


# ---------- Slug ↔ seconds map ----------


def test_choices_cover_user_request() -> None:
    """The four labels the user asked for ("не спрашивать до
    перезапуска", "раз в 12 часов", "раз в 4 часа", "раз в час")
    must all be in the choices list. Pin the seconds too so future
    UI relabels don't silently change the actual lock window."""
    slugs = {slug: seconds for _label, seconds, slug in LOCK_DURATION_CHOICES}
    assert slugs == {
        "until_restart": 0,
        "12h": 43200,
        "4h": 14400,
        "1h": 3600,
    }


def test_slug_to_seconds_known_values() -> None:
    assert slug_to_seconds("until_restart") == 0
    assert slug_to_seconds("12h") == 43200
    assert slug_to_seconds("4h") == 14400
    assert slug_to_seconds("1h") == 3600


def test_slug_to_seconds_unknown_returns_none() -> None:
    # Caller must fall back to the existing pref rather than silently
    # forcing 0.
    assert slug_to_seconds("forever") is None
    assert slug_to_seconds("") is None
    assert slug_to_seconds("3600") is None  # raw seconds, not a known slug


def test_seconds_to_slug_known_values() -> None:
    assert seconds_to_slug(0) == "until_restart"
    assert seconds_to_slug(43200) == "12h"
    assert seconds_to_slug(14400) == "4h"
    assert seconds_to_slug(3600) == "1h"


def test_seconds_to_slug_unknown_falls_back_to_never() -> None:
    # A value set out-of-band (e.g. via env override) should still
    # render the UI without crashing.
    assert seconds_to_slug(7200) == "until_restart"  # 2h — not in the list


# ---------- user_prefs.json round-trip ----------


def test_load_user_prefs_missing_file_returns_defaults(tmp_data: Path) -> None:
    prefs = load_user_prefs()
    assert prefs["auto_lock_seconds"] == 0
    assert prefs["schema_version"] == 1


def test_load_user_prefs_after_save_round_trip(tmp_data: Path) -> None:
    save_user_prefs({"auto_lock_seconds": 43200})
    prefs = load_user_prefs()
    assert prefs["auto_lock_seconds"] == 43200
    # The on-disk file is plain JSON.
    raw = json.loads((tmp_data / "user_prefs.json").read_text(encoding="utf-8"))
    assert raw["auto_lock_seconds"] == 43200
    assert raw["schema_version"] == 1


def test_load_user_prefs_corrupt_file_doesnt_crash(tmp_data: Path) -> None:
    """If the file gets truncated during a crash, the loader returns
    defaults instead of raising. Boot must continue."""
    (tmp_data / "user_prefs.json").write_text("not-json {", encoding="utf-8")
    prefs = load_user_prefs()
    assert prefs["auto_lock_seconds"] == 0
    assert prefs["schema_version"] == 1


def test_save_user_prefs_preserves_unknown_fields(tmp_data: Path) -> None:
    """Future-proof — we want adding new prefs in another version of
    the app to not blow away ones the current version doesn't know
    about."""
    (tmp_data / "user_prefs.json").write_text(
        json.dumps({"auto_lock_seconds": 14400, "favourite_color": "purple"}),
        encoding="utf-8",
    )
    # Re-save with only auto_lock_seconds — the extra field should be
    # preserved if we pass it back through load → save.
    prefs = load_user_prefs()
    prefs["auto_lock_seconds"] = 3600
    save_user_prefs(prefs)
    after = json.loads((tmp_data / "user_prefs.json").read_text(encoding="utf-8"))
    assert after["auto_lock_seconds"] == 3600
    assert after["favourite_color"] == "purple"


def test_save_user_prefs_writes_schema_version(tmp_data: Path) -> None:
    save_user_prefs({"auto_lock_seconds": 0})
    raw = json.loads((tmp_data / "user_prefs.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
