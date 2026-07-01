"""Attach sender + me information to a chat's ``messages.json``.

tdl's ``chat export`` JSON only carries ``id, date, text, file`` per
message — it lacks ``from_id``. We need that to render a TG-Desktop-
style transcript with left/right column splits and per-speaker
avatars.

This module reads ``messages.json``, queries Telethon for each
message id (chunked), writes ``from_id`` back into the JSON, and
downloads the user's own profile photo so the renderer can use it.

Usage from the orchestrator (composition wiring) — not from tests:

    from src.jobs.history_enrich import enrich_history
    await enrich_history(
        chat_id=216281752,
        messages_json=Path('exports/chat_X/messages.json'),
        avatars_dir=Path('data/avatars'),
        manager=app.state.telegram_manager,
    )

The function is idempotent: messages that already have ``from_id``
are skipped, so re-running after an incremental download only
costs API calls for the new ids.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.logging_setup import get_logger

log = get_logger(__name__)


_BATCH_SIZE = 100  # Telethon supports up to 200 ids/call; 100 = safer
_FLOOD_BACKOFF_FALLBACK_SECONDS = 30
# How often to flush messages.json so a long enrich is restart-safe.
_FLUSH_EVERY_N_MESSAGES = 500


@dataclass(slots=True)
class EnrichStats:
    total: int
    enriched: int
    skipped: int
    me_id: int | None
    me_avatar_path: Path | None


async def enrich_history(
    *,
    chat_id: int,
    messages_json: Path,
    avatars_dir: Path,
    manager: Any,
) -> EnrichStats:
    """Add ``from_id`` to every message in ``messages_json`` + grab
    the logged-in user's avatar so the HTML renderer can split bubbles
    into ``in`` / ``out`` columns.

    Pure no-op when there's nothing to do (file missing / Telethon
    not authorised). Returns counters so the caller can log them.
    """
    if not messages_json.exists():
        return EnrichStats(0, 0, 0, None, None)

    me_id = await manager.me_id()
    if me_id is None:
        log.info("enrich_history_skipped_no_me_id", chat_id=chat_id)
        return EnrichStats(0, 0, 0, None, None)

    # Always make sure we have the current user's avatar on disk.
    avatars_dir.mkdir(parents=True, exist_ok=True)
    me_avatar = avatars_dir / f"{me_id}.jpg"
    if not me_avatar.exists() or me_avatar.stat().st_size == 0:
        try:
            path = await manager.download_avatar(me_id, me_avatar)
            if path is None:
                me_avatar.write_bytes(b"")
        except Exception as exc:  # noqa: BLE001
            log.warning("me_avatar_download_failed", error=str(exc))

    data = json.loads(messages_json.read_text(encoding="utf-8"))
    msgs: list[dict[str, Any]] = data.get("messages") or []
    needing: list[int] = []
    for m in msgs:
        if "from_id" in m and m["from_id"] is not None:
            continue
        mid = m.get("id") or m.get("ID")
        if isinstance(mid, int):
            needing.append(mid)

    log.info(
        "enrich_history_start",
        chat_id=chat_id,
        total=len(msgs),
        needing_enrichment=len(needing),
        me_id=me_id,
    )
    if not needing:
        return EnrichStats(len(msgs), 0, len(msgs), me_id, me_avatar)

    by_id: dict[int, dict[str, Any]] = {
        m["id"]: m for m in msgs if isinstance(m.get("id"), int)
    }

    def _flush() -> None:
        data["me_id"] = me_id
        data["messages"] = sorted(
            by_id.values(), key=lambda m: m.get("id", 0), reverse=True
        )
        messages_json.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    enriched = 0
    needing_set = set(needing)
    # Single streaming pass via Telethon's `iter_messages` (no `ids=`).
    # This translates to `messages.getHistory` chunks of ~100 — its
    # rate limit is far gentler than per-id `getMessages`. We bail
    # early when every needing-id has been seen.
    try:
        async for mid, sid in manager.iter_message_senders(chat_id, ids=None):
            if mid in by_id and by_id[mid].get("from_id") is None:
                by_id[mid]["from_id"] = _normalise_peer(sid)
                enriched += 1
                if mid in needing_set:
                    needing_set.discard(mid)
                if enriched % _FLUSH_EVERY_N_MESSAGES == 0:
                    _flush()
                    log.info(
                        "enrich_history_progress",
                        chat_id=chat_id,
                        enriched=enriched,
                        remaining=len(needing_set),
                    )
                if not needing_set:
                    break
    except Exception as exc:  # noqa: BLE001
        wait = _extract_flood_seconds(exc) or _FLOOD_BACKOFF_FALLBACK_SECONDS
        log.warning(
            "enrich_history_floodwait",
            chat_id=chat_id,
            wait_seconds=wait,
            error=str(exc)[:200],
        )
        # Persist what we have before sleeping; caller can re-invoke.
        _flush()
        await asyncio.sleep(wait)

    _flush()
    log.info(
        "enrich_history_done",
        chat_id=chat_id,
        enriched=enriched,
        total=len(msgs),
    )
    return EnrichStats(
        total=len(msgs),
        enriched=enriched,
        skipped=len(msgs) - enriched,
        me_id=me_id,
        me_avatar_path=me_avatar,
    )


def _normalise_peer(peer: Any) -> int | None:
    """Telethon yields ``sender_id`` as either an int (newer versions)
    or a Peer object (older). Always return a plain int (or None).
    """
    if peer is None:
        return None
    if isinstance(peer, int):
        return peer
    # Peer*-like object: try common attribute names.
    for attr in ("user_id", "channel_id", "chat_id"):
        v = getattr(peer, attr, None)
        if isinstance(v, int):
            return v
    return None


def _extract_flood_seconds(exc: Exception) -> int | None:
    """Pull the wait duration out of a Telethon FloodWaitError if we can.

    Telethon's exception exposes ``.seconds``. We don't import the
    class to avoid a hard dependency cycle in tests; duck-type it.
    """
    seconds = getattr(exc, "seconds", None)
    if isinstance(seconds, int):
        return min(seconds, 600)  # cap at 10 min so we don't sleep forever
    return None
