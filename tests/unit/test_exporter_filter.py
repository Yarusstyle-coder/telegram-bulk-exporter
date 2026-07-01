"""Unit tests for the manifest filter + slug helper."""

from __future__ import annotations

import json
from pathlib import Path

from src.jobs.exporter import (
    _chat_slug,
    _filter_manifest,
    _tdl_chat_handle_candidates,
    _to_tdl_id,
)
from src.jobs.models import MediaType


def test_to_tdl_id_strips_minus_100_for_channels() -> None:
    # Telethon channel id: -1009876543210 → tdl raw: 9876543210
    assert _to_tdl_id(-1009876543210) == 9876543210
    # Another real value:
    assert _to_tdl_id(-1005551237890) == 5551237890


def test_to_tdl_id_keeps_user_and_basic_group() -> None:
    # User / bot ids stay positive
    assert _to_tdl_id(7712345678) == 7712345678
    # Basic group (negative but small magnitude) stays as-is
    assert _to_tdl_id(-212003) == -212003


def test_tdl_chat_handle_candidates_username_no_fallback() -> None:
    # A @username already resolves server-side; no point trying junk
    # numeric ids if Telegram doesn't recognise the username.
    assert _tdl_chat_handle_candidates("@my_chat") == ["@my_chat"]


def test_tdl_chat_handle_candidates_user_id_no_fallback() -> None:
    # User/bot ids are positive — no alternative format exists.
    assert _tdl_chat_handle_candidates(12345) == [12345]


def test_tdl_chat_handle_candidates_basic_group_includes_supergroup_form() -> None:
    # Real user case: "Тестовая комната" had id=-5566778899 (basic group
    # by Telethon) but tdl couldn't resolve. We try -100<abs(id)>
    # (supergroup-style) and raw positive as fallbacks.
    cands = _tdl_chat_handle_candidates(-5566778899)
    assert cands[0] == -5566778899
    assert -1005566778899 in cands  # -10**12 - 5566778899
    assert 5566778899 in cands


def test_tdl_chat_handle_candidates_supergroup_id_unchanged() -> None:
    # Supergroup id (Telethon already strips -100 in _to_tdl_id, so
    # this helper sees the raw positive id; no extra candidates).
    cands = _tdl_chat_handle_candidates(5551237890)
    assert cands == [5551237890]


def test_tdl_chat_handle_candidates_dedup_when_id_is_zero() -> None:
    # Edge: 0 has no useful alternates — same id wouldn't be added twice.
    cands = _tdl_chat_handle_candidates(0)
    assert cands == [0]


def _write(p: Path, data: dict) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_chat_slug_stable_without_title() -> None:
    assert _chat_slug(12345) == "chat_12345"
    assert _chat_slug(-1001234) == "chat_-1001234"


def test_chat_slug_with_title_includes_slug_and_id() -> None:
    # English title
    assert _chat_slug(12345, "Black Room") == "chat_Black_Room_12345"
    # Cyrillic transliteration via python-slugify
    slug = _chat_slug(-1009876543210, "Тестовая комната")
    assert slug.startswith("chat_") and slug.endswith("_-1009876543210")
    assert "12345" not in slug


def test_chat_slug_with_emojis_and_special_chars_falls_back_safely() -> None:
    # Emoji / pure-symbol titles produce empty slugs → fall back to id only.
    slug = _chat_slug(42, "🎉")
    assert slug == "chat_42"


def test_chat_slug_caps_length_for_windows_path() -> None:
    long_title = "Very Long Title " * 20  # ≈340 chars
    slug = _chat_slug(7, long_title)
    # 60-char cap on the slug + "chat_" + "_7" → well under 260.
    assert len(slug) <= 80


def test_filter_keeps_only_allowed_media(tmp_path: Path) -> None:
    """tdl JSON uses CamelCase (ID/Date/Media.Name) and infers media kind
    from the file extension, not from a class field."""
    raw = _write(
        tmp_path / "raw.json",
        {
            "messages": [
                {"ID": 1, "Media": {"Name": "photo.jpg", "Size": 100}},
                {"ID": 2, "Media": {"Name": "clip.mp4", "Size": 200}},
                {"ID": 3, "Media": {"Name": "sticker.tgs", "Size": 50}},
                {"ID": 4, "Message": "plain"},
            ]
        },
    )
    out = tmp_path / "manifest.json"
    count, max_id = _filter_manifest(raw, out, media_types=[MediaType.PHOTO], max_size=None)
    assert count == 1
    assert max_id == 4
    payload = json.loads(out.read_text())
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["ID"] == 1


def test_filter_respects_size_cap(tmp_path: Path) -> None:
    raw = _write(
        tmp_path / "raw.json",
        {
            "messages": [
                {"ID": 10, "Media": {"Name": "small.mp4", "Size": 1_000_000}},
                {"ID": 11, "Media": {"Name": "big.mp4",   "Size": 10_000_000}},
            ]
        },
    )
    out = tmp_path / "manifest.json"
    count, _ = _filter_manifest(
        raw,
        out,
        media_types=[MediaType.VIDEO],
        max_size=5_000_000,
    )
    assert count == 1


def test_filter_handles_missing_or_empty(tmp_path: Path) -> None:
    raw = tmp_path / "missing.json"
    out = tmp_path / "manifest.json"
    count, max_id = _filter_manifest(raw, out, media_types=[MediaType.PHOTO], max_size=None)
    assert count == 0
    assert max_id is None


def test_filter_handles_malformed_json(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    raw.write_text("{ not json")
    out = tmp_path / "manifest.json"
    count, max_id = _filter_manifest(raw, out, media_types=[MediaType.PHOTO], max_size=None)
    assert count == 0
    assert max_id is None


def test_filter_preserves_multiple_media_types(tmp_path: Path) -> None:
    raw = _write(
        tmp_path / "raw.json",
        {
            "messages": [
                {"ID": 101, "Media": {"Name": "x.jpg",  "Size": 1}},
                {"ID": 102, "Media": {"Name": "y.mp3",  "Size": 2}},
                {"ID": 103, "Media": {"Name": "z.tgs",  "Size": 3}},
            ]
        },
    )
    out = tmp_path / "m.json"
    count, _ = _filter_manifest(
        raw, out, media_types=[MediaType.PHOTO, MediaType.AUDIO], max_size=None
    )
    assert count == 2


def test_filter_date_window(tmp_path: Path) -> None:
    raw = _write(
        tmp_path / "raw.json",
        {
            "messages": [
                {"ID": 1, "Date": 1577836800,  "Media": {"Name": "old.jpg",   "Size": 1}},
                {"ID": 2, "Date": 1735689600,  "Media": {"Name": "fresh.jpg", "Size": 2}},
            ]
        },
    )
    out = tmp_path / "m.json"
    # Only after 2024-01-01 → keeps only #2
    count, _ = _filter_manifest(
        raw, out,
        media_types=[MediaType.PHOTO], max_size=None,
        date_from="2024-01-01",
    )
    assert count == 1


def test_filter_minimal_tdl_format(tmp_path: Path) -> None:
    """tdl chat export WITHOUT --all emits minimal JSON: {id, type, file}.
    `file` is a plain string filename (no Size/DC). The filter must
    accept that and infer the type from the extension."""
    raw = _write(
        tmp_path / "raw.json",
        {
            "id": 5551237890,
            "type": "channel",
            "messages": [
                # Real shape observed from tdl 0.20.2 — `file` is a string.
                {"id": 22383, "type": "channel", "file": "p.jpg"},
                {"id": 22388, "type": "channel", "file": "v.mp4"},
                {"id": 22389, "type": "channel", "file": "doc.pdf"},
            ],
        },
    )
    out = tmp_path / "m.json"
    count, max_id = _filter_manifest(
        raw, out, media_types=[MediaType.PHOTO, MediaType.VIDEO], max_size=None
    )
    assert count == 2  # pdf excluded
    assert max_id == 22389
    payload = json.loads(out.read_text())
    assert payload["id"] == 5551237890
    assert payload["type"] == "channel"


def test_filter_keeps_top_level_fields(tmp_path: Path) -> None:
    """Critical for tdl dl: id/type at the top level must survive filtering."""
    raw = _write(
        tmp_path / "raw.json",
        {
            "id": 1234,
            "type": "private",
            "messages": [
                {"ID": 1, "Media": {"Name": "p.jpg", "Size": 10}},
            ],
        },
    )
    out = tmp_path / "m.json"
    _filter_manifest(raw, out, media_types=[MediaType.PHOTO], max_size=None)
    payload = json.loads(out.read_text())
    assert payload["id"] == 1234
    assert payload["type"] == "private"
    assert len(payload["messages"]) == 1
