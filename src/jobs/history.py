"""Persist + render the message history of one chat.

Two responsibilities:

1. :func:`persist_message_history` — merge the latest tdl-chat-export
   JSON into ``messages.json`` next to the media folder, deduping by
   message id so incremental runs accumulate the full history.

2. :func:`render_history_html` — turn that JSON into a single
   ``messages.html`` in **Telegram-Desktop's exported format**, so the
   bundled editor (``/static/tg_export/editor.html``) can drag-and-
   drop or auto-load it without any translation. The HTML references
   ``../../static/tg_export/style.css`` + ``../../static/tg_export/
   script.js`` for the real TG Desktop client stylesheet + JS.

File-naming contract:
    tdl ``dl -f manifest.json -d media/`` writes every file as
    ``<chat_id>_<msg_id>_<original_name>``. The ``"file"`` field in
    the raw JSON only holds the *original* name, so we compose the
    on-disk path here as ``media/<chat_id>_<msg_id>_<file>``.

Sender info: when ``messages.json`` carries ``me_id`` + per-message
``from_id`` (added by :mod:`history_enrich`), bubbles split across
the two participants, with their own avatars/initials. Without that
metadata we still render every message in a flat list — the editor
remains usable, just without speaker bubbles.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.logging_setup import get_logger

log = get_logger(__name__)


_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "heic", "heif"}
_VIDEO_EXTS = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}
_VOICE_EXTS = {"ogg", "oga", "opus"}
_AUDIO_EXTS = {"mp3", "m4a", "flac", "wav", "aac"}
_STICKER_WEBP = {"webp"}
_STICKER_TGS = {"tgs"}

_MONTHS_EN = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def persist_message_history(
    raw_json: Path, dest: Path, *, title: str | None = None
) -> int:
    """Merge ``raw_json`` into ``dest`` and return the resulting count.

    Both files share tdl's ``{id, messages: [...]}`` shape. ``id`` is
    the chat id and is preserved from the existing file when present.
    Returns 0 on a missing / unparseable raw input rather than raising.
    """
    if not raw_json.exists() or raw_json.stat().st_size == 0:
        return 0

    try:
        new = json.loads(raw_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("history_raw_unparseable", path=str(raw_json))
        return 0

    new_messages = new.get("messages") or new.get("Messages") or []
    if not isinstance(new_messages, list):
        new_messages = []

    existing_messages: list[dict] = []
    chat_id = new.get("id") or new.get("ID")
    if dest.exists():
        try:
            old = json.loads(dest.read_text(encoding="utf-8"))
            existing_messages = old.get("messages") or []
            if not isinstance(existing_messages, list):
                existing_messages = []
            chat_id = old.get("id") or chat_id
        except json.JSONDecodeError:
            log.warning("history_existing_unparseable", path=str(dest))

    by_id: dict[int, dict] = {}
    for m in existing_messages:
        mid = m.get("id") or m.get("ID")
        if isinstance(mid, int):
            by_id[mid] = m
    for m in new_messages:
        mid = m.get("id") or m.get("ID")
        if isinstance(mid, int):
            by_id[mid] = m

    merged = sorted(by_id.values(), key=lambda m: m.get("id", 0), reverse=True)
    out: dict[str, Any] = {"id": chat_id, "messages": merged}
    if title:
        out["title"] = title
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(merged)


def render_history_html(
    src_json: Path,
    dest_html: Path,
    *,
    title: str | None = None,
    avatars_dir: Path | None = None,  # noqa: ARG001 — kept for API stability
    static_url_prefix: str = "../../static/tg_export",
) -> None:
    """Render ``src_json`` into a Telegram-Desktop-format messages.html.

    ``static_url_prefix`` is the URL prefix where ``style.css`` /
    ``script.js`` are served from. The default ``../../static/...`` is
    correct when the file is opened through the FastAPI app at
    ``/history/{chat_id}/file/messages.html`` (the prefix walks up
    from ``/history/{id}/file/`` to ``/static/tg_export/``).
    """
    data = json.loads(src_json.read_text(encoding="utf-8"))
    chat_id = data.get("id") or data.get("ID")
    me_id = data.get("me_id")
    messages = data.get("messages") or []
    chat_title = title or data.get("title") or f"chat {chat_id or ''}"

    sorted_msgs = sorted(messages, key=lambda m: m.get("id", 0))

    # TG Desktop has 8 colour slots (userpic1..userpic8). Pin "you" → 6
    # and the peer → 8 for a stable two-tone palette.
    me_class = "userpic6"
    peer_class = "userpic8"
    me_initials = "Я"
    peer_initials = _initials_from_title(chat_title)

    parts: list[str] = []
    parts.append(_HEADER_TEMPLATE.format(
        title=html.escape(chat_title),
        static=static_url_prefix.rstrip("/"),
    ))
    parts.append('<div class="history">\n')

    last_speaker: int | None = None
    last_day: str = ""
    for m in sorted_msgs:
        ts = m.get("date") or m.get("Date")
        if not isinstance(ts, (int, float)):
            continue
        dt = datetime.fromtimestamp(int(ts), tz=UTC)
        day_key = dt.strftime("%Y-%m-%d")
        if day_key != last_day:
            parts.append(_render_service_day(dt))
            last_day = day_key
            last_speaker = None  # day break breaks the streak

        from_id = m.get("from_id")
        is_out = me_id is not None and from_id == me_id
        joined = (
            from_id is not None and last_speaker is not None
            and from_id == last_speaker
        )
        speaker_initials = me_initials if is_out else peer_initials
        speaker_class = me_class if is_out else peer_class
        speaker_name = "Я" if is_out else (chat_title or "")

        msg_html = _render_message(
            m,
            dt,
            chat_id=chat_id,
            joined=joined,
            speaker_initials=speaker_initials,
            speaker_class=speaker_class,
            speaker_name=speaker_name,
        )
        if msg_html:
            parts.append(msg_html)
            last_speaker = from_id

    parts.append("</div>\n")  # /.history
    parts.append(_FOOTER)
    dest_html.write_text("".join(parts), encoding="utf-8")


def _initials_from_title(title: str) -> str:
    pieces = [p for p in title.split() if p and p[0].isalpha()]
    if not pieces:
        return "?"
    if len(pieces) == 1:
        return pieces[0][:2].upper()
    return (pieces[0][0] + pieces[1][0]).upper()


def _render_service_day(dt: datetime) -> str:
    label = f"{dt.day} {_MONTHS_EN[dt.month]} {dt.year}"
    return (
        f'<div class="message service">\n'
        f'<div class="body details">\n{html.escape(label)}\n</div>\n'
        f'</div>\n'
    )


def _render_message(
    m: dict,
    dt: datetime,
    *,
    chat_id: int | None,
    joined: bool,
    speaker_initials: str,
    speaker_class: str,
    speaker_name: str,
) -> str:
    mid = m.get("id") or m.get("ID") or 0
    text = m.get("text") or m.get("Message") or m.get("message") or ""
    file_name = m.get("file") or ""
    if not text and not file_name:
        return ""

    classes = ["message", "default", "clearfix"]
    if joined:
        classes.append("joined")
    pretty_ts = dt.strftime("%d.%m.%Y %H:%M:%S UTC")

    out = [f'<div class="{" ".join(classes)}" id="message{mid}">\n']
    if not joined:
        out.append(
            f'<div class="pull_left userpic_wrap">\n'
            f'<div class="userpic {speaker_class}" '
            f'style="width: 42px; height: 42px">\n'
            f'<div class="initials" style="line-height: 42px">\n'
            f'{html.escape(speaker_initials)}\n</div>\n</div>\n</div>\n'
        )
    out.append('<div class="body">\n')
    out.append(
        f'<div class="pull_right date details" title="{html.escape(pretty_ts)}">\n'
        f'{dt.strftime("%H:%M")}\n</div>\n'
    )
    if not joined and speaker_name:
        out.append(
            f'<div class="from_name">\n{html.escape(speaker_name)}\n</div>\n'
        )
    if file_name:
        out.append(_render_attachment(str(file_name), chat_id, mid))
    if text:
        out.append(
            f'<div class="text">\n{_escape_text(str(text))}\n</div>\n'
        )
    out.append('</div>\n')  # /.body
    out.append('</div>\n')  # /.message
    return "".join(out)


def _render_attachment(file_name: str, chat_id: int | None, msg_id: int) -> str:
    """Inline preview for one attachment — emits TG Desktop classes
    (``.media_wrap``, ``.photo_wrap``, ``.video_file_wrap``, etc.) so
    the bundled style.css picks the right styling.
    """
    name = file_name.strip()
    if not name:
        return ""
    on_disk = (
        f"{chat_id}_{msg_id}_{name}"
        if (chat_id is not None and msg_id)
        else name
    )
    rel = f"media/{html.escape(on_disk, quote=True)}"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    title_e = html.escape(name)

    if ext in _IMAGE_EXTS:
        return (
            f'<div class="media_wrap clearfix">\n'
            f'<a class="photo_wrap clearfix pull_left" href="{rel}">\n'
            f'<img class="photo" src="{rel}" '
            f'style="max-width:260px;max-height:260px" loading="lazy">\n'
            f'</a>\n</div>\n'
        )
    if ext in _VIDEO_EXTS:
        return (
            f'<div class="media_wrap clearfix">\n'
            f'<a class="video_file_wrap clearfix pull_left" href="{rel}">\n'
            f'<div class="video_file">\n'
            f'<video controls preload="metadata" src="{rel}" '
            f'style="max-width:360px;max-height:360px"></video>\n'
            f'</div>\n</a>\n</div>\n'
        )
    if ext in _VOICE_EXTS:
        return (
            f'<div class="media_wrap clearfix">\n'
            f'<a class="voice_message_wrap clearfix pull_left" href="{rel}">\n'
            f'<audio controls preload="metadata" src="{rel}"></audio>\n'
            f'</a>\n</div>\n'
        )
    if ext in _AUDIO_EXTS:
        return (
            f'<div class="media_wrap clearfix">\n'
            f'<a class="audio_file_wrap clearfix pull_left" href="{rel}">\n'
            f'<audio controls preload="metadata" src="{rel}"></audio>\n'
            f'</a>\n</div>\n'
        )
    if ext in _STICKER_WEBP:
        return (
            f'<div class="media_wrap clearfix">\n'
            f'<a class="sticker_wrap clearfix pull_left" href="{rel}">\n'
            f'<img class="sticker" src="{rel}" style="max-width:128px" '
            f'loading="lazy">\n</a>\n</div>\n'
        )
    if ext in _STICKER_TGS:
        return (
            f'<div class="media_wrap clearfix">\n'
            f'<a class="sticker_wrap clearfix pull_left" href="{rel}" '
            f'title="{title_e}">🎬 sticker</a>\n</div>\n'
        )
    return (
        f'<div class="media_wrap clearfix">\n'
        f'<a class="media_wrap clearfix pull_left" href="{rel}">\n'
        f'<div class="media clearfix pull_left">\n'
        f'<div class="title bold">📎 {title_e}</div>\n'
        f'</div>\n</a>\n</div>\n'
    )


def _escape_text(text: str) -> str:
    """Escape, preserve newlines, autolink http(s)://…"""
    escaped = html.escape(text).replace("\n", "<br>")
    out: list[str] = []
    in_url = False
    buf: list[str] = []
    i = 0
    while i < len(escaped):
        if not in_url and (
            escaped.startswith("http://", i) or escaped.startswith("https://", i)
        ):
            in_url = True
            buf = []
        if in_url:
            ch = escaped[i]
            if ch in (" ", "<", "\t", "\n"):
                in_url = False
                url = "".join(buf)
                out.append(
                    f'<a href="{url}" target="_blank" rel="noopener">{url}</a>'
                )
                buf = []
                out.append(ch)
            else:
                buf.append(ch)
        else:
            out.append(escaped[i])
        i += 1
    if in_url:
        url = "".join(buf)
        out.append(f'<a href="{url}" target="_blank" rel="noopener">{url}</a>')
    return "".join(out)


def merge_message_iter(messages: Iterable[dict]) -> list[dict]:
    """Test helper — same dedupe/sort logic as ``persist_message_history``
    but on an in-memory iterable.
    """
    by_id: dict[int, dict] = {}
    for m in messages:
        mid = m.get("id") or m.get("ID")
        if isinstance(mid, int):
            by_id[mid] = m
    return sorted(by_id.values(), key=lambda m: m.get("id", 0), reverse=True)


_HEADER_TEMPLATE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>{title}</title>
<meta content="width=device-width, initial-scale=1.0" name="viewport">
<link href="{static}/style.css" rel="stylesheet">
<script src="{static}/script.js" type="text/javascript"></script>
</head><body>
<div class="page_wrap">
<div class="page_header">
<div class="content">
<div class="text bold">
{title}
</div>
</div>
</div>
<div class="page_body chat_page">
"""

_FOOTER = """</div>
</div>
</body></html>
"""
