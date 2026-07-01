"""Unit tests for src.jobs.history.render_history_html.

The renderer must emit Telegram-Desktop-compatible HTML so that the
bundled editor (``/static/tg_export/editor.html``) and the TG Desktop
``style.css`` + ``script.js`` style and script the page consistently.

What we check:

* The wrapper structure is ``page_wrap > page_body.chat_page > history``.
* Each message is a ``.message.default.clearfix`` (joined when the
  same sender speaks consecutively, no joined for the first / day-break).
* Day separators are ``.message.service`` and reset the ``joined`` streak.
* Speaker bubbles split on ``from_id`` vs ``me_id``: outgoing → userpic6
  with initials "Я", incoming → userpic8 with initials from the chat
  title.
* Attachments emit the right TG Desktop wrapper class for their kind
  (.photo_wrap / .video_file_wrap / .voice_message_wrap / etc).
* Static assets are referenced via the ``../../static/tg_export/``
  prefix by default, so opening through ``/history/{id}/file/messages.html``
  resolves to ``/static/tg_export/style.css``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.jobs.history import render_history_html


def _write_msgs(tmp_path: Path, messages: list[dict], *, me_id: int | None = None,
                title: str = "Test Chat", chat_id: int = 9999) -> Path:
    src = tmp_path / "messages.json"
    payload: dict = {"id": chat_id, "messages": messages, "title": title}
    if me_id is not None:
        payload["me_id"] = me_id
    src.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return src


def test_render_emits_tg_desktop_skeleton(tmp_path: Path) -> None:
    src = _write_msgs(tmp_path, [
        {"id": 1, "date": 1700000000, "from_id": 100, "text": "hi"},
    ], me_id=200, title="Alice")
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice")
    body = dest.read_text(encoding="utf-8")

    assert '<div class="page_wrap">' in body
    assert '<div class="page_body chat_page">' in body
    assert '<div class="history">' in body
    assert '../../static/tg_export/style.css' in body
    assert '../../static/tg_export/script.js' in body


def test_outgoing_vs_incoming_speakers(tmp_path: Path) -> None:
    src = _write_msgs(tmp_path, [
        {"id": 1, "date": 1700000000, "from_id": 100, "text": "from peer"},
        {"id": 2, "date": 1700000060, "from_id": 200, "text": "from me"},
    ], me_id=200, title="Alice")
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice")
    body = dest.read_text(encoding="utf-8")

    # Peer block (incoming) → userpic8 + chat-title initials
    assert 'userpic userpic8' in body
    # My block (outgoing) → userpic6 + "Я"
    assert 'userpic userpic6' in body
    assert '>\nЯ\n<' in body or '>Я<' in body


def test_joined_streak_for_same_sender(tmp_path: Path) -> None:
    src = _write_msgs(tmp_path, [
        {"id": 1, "date": 1700000000, "from_id": 100, "text": "one"},
        {"id": 2, "date": 1700000005, "from_id": 100, "text": "two"},
        {"id": 3, "date": 1700000010, "from_id": 200, "text": "three"},
    ], me_id=200, title="Alice")
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice")
    body = dest.read_text(encoding="utf-8")

    # First message → not joined. Second → joined. Third → not joined (different sender).
    assert 'id="message1"' in body
    assert 'id="message2"' in body
    assert 'id="message3"' in body
    # Find the message2 div and check it has joined class
    m2 = body.split('id="message2"')[0].rsplit('<div class="message', 1)[-1]
    assert 'joined' in m2, f"Expected 'joined' on m2 div header, got: {m2!r}"


def test_day_separator_resets_join_streak(tmp_path: Path) -> None:
    # 1700000000 is 2023-11-14, 1700100000 is 2023-11-16 — different days.
    src = _write_msgs(tmp_path, [
        {"id": 1, "date": 1700000000, "from_id": 100, "text": "day1"},
        {"id": 2, "date": 1700100000, "from_id": 100, "text": "day2"},
    ], me_id=200, title="Alice")
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice")
    body = dest.read_text(encoding="utf-8")

    # Two distinct service-day separators.
    assert body.count('class="message service"') == 2
    # m2 must NOT be joined despite same from_id, because day break reset the streak
    m2 = body.split('id="message2"')[0].rsplit('<div class="message', 1)[-1]
    assert 'joined' not in m2


def test_image_attachment_uses_photo_wrap(tmp_path: Path) -> None:
    src = _write_msgs(tmp_path, [
        {"id": 42, "date": 1700000000, "from_id": 100, "file": "kitten.jpg"},
    ], me_id=200, title="Alice", chat_id=777)
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice")
    body = dest.read_text(encoding="utf-8")

    assert 'class="photo_wrap clearfix pull_left"' in body
    # File path is composed as media/<chat_id>_<msg_id>_<name>
    assert 'media/777_42_kitten.jpg' in body


def test_voice_attachment_uses_voice_message_wrap(tmp_path: Path) -> None:
    src = _write_msgs(tmp_path, [
        {"id": 1, "date": 1700000000, "from_id": 100, "file": "audio.ogg"},
    ], me_id=200, title="Alice", chat_id=777)
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice")
    body = dest.read_text(encoding="utf-8")

    assert 'class="voice_message_wrap' in body


def test_video_attachment_uses_video_file_wrap(tmp_path: Path) -> None:
    src = _write_msgs(tmp_path, [
        {"id": 1, "date": 1700000000, "from_id": 100, "file": "clip.mp4"},
    ], me_id=200, title="Alice", chat_id=777)
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice")
    body = dest.read_text(encoding="utf-8")

    assert 'class="video_file_wrap' in body


def test_text_only_message_renders_without_attachment_wrappers(tmp_path: Path) -> None:
    src = _write_msgs(tmp_path, [
        {"id": 1, "date": 1700000000, "from_id": 100, "text": "hello world"},
    ], me_id=200, title="Alice")
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice")
    body = dest.read_text(encoding="utf-8")

    assert 'hello world' in body
    assert 'photo_wrap' not in body
    assert 'video_file_wrap' not in body


def test_url_in_text_gets_autolinked(tmp_path: Path) -> None:
    src = _write_msgs(tmp_path, [
        {"id": 1, "date": 1700000000, "from_id": 100,
         "text": "see https://example.com/foo for details"},
    ], me_id=200, title="Alice")
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice")
    body = dest.read_text(encoding="utf-8")

    assert '<a href="https://example.com/foo"' in body
    assert 'target="_blank"' in body


def test_empty_message_is_skipped(tmp_path: Path) -> None:
    src = _write_msgs(tmp_path, [
        {"id": 1, "date": 1700000000, "from_id": 100, "text": "", "file": ""},
        {"id": 2, "date": 1700000060, "from_id": 100, "text": "real"},
    ], me_id=200, title="Alice")
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice")
    body = dest.read_text(encoding="utf-8")

    assert 'id="message1"' not in body
    assert 'id="message2"' in body


def test_static_url_prefix_overridable(tmp_path: Path) -> None:
    src = _write_msgs(tmp_path, [
        {"id": 1, "date": 1700000000, "from_id": 100, "text": "hi"},
    ], me_id=200, title="Alice")
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice",
                        static_url_prefix="/static/tg_export")
    body = dest.read_text(encoding="utf-8")

    assert '/static/tg_export/style.css' in body
    assert '../../static/tg_export/style.css' not in body


@pytest.mark.parametrize("ts,expected_label", [
    (1700000000, "14 November 2023"),  # 2023-11-14 22:13 UTC
    (1722643200, "3 August 2024"),     # 2024-08-03
])
def test_service_day_label_uses_english_month(tmp_path: Path,
                                              ts: int, expected_label: str) -> None:
    src = _write_msgs(tmp_path, [
        {"id": 1, "date": ts, "from_id": 100, "text": "x"},
    ], me_id=200, title="Alice")
    dest = tmp_path / "messages.html"
    render_history_html(src, dest, title="Alice")
    body = dest.read_text(encoding="utf-8")

    assert expected_label in body
