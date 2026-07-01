"""Chat-list HTTP routes.

Two responsibilities:

- GET /chats          → HTML grid of dialogs (HTMX target for search).
- POST /chats/refresh → triggers a Telethon dialog re-fetch; returns the grid
                         fragment for the caller to swap in.

Telethon calls are mediated by a singleton `TelegramSessionManager` which is
placed on `app.state` during lifespan. The routes are tolerant of a missing
manager: if the user hasn't authenticated against Telegram yet, they see a
"connect Telegram" card instead of an empty list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from src.api._chats_refresh_stamp import (
    humanise_refreshed,
    read_refresh_stamp,
    write_refresh_stamp,
)
from src.db.models import Chat, ChatState
from src.logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/chats", tags=["chats"])


def _templates(request: Request) -> Any:
    return request.app.state.templates


def _session_factory(request: Request) -> Any | None:
    return getattr(request.app.state, "session_factory", None)


def _tg_manager(request: Request) -> Any | None:
    return getattr(request.app.state, "telegram_manager", None)


def _coerce_int(raw: str | None) -> int | None:
    """Tolerant int parser for query params: '', None, 'null', 'undefined' → None."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("null", "none", "undefined"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


# ё / е are interchangeable in informal Russian — many channel/group
# titles drop the diaeresis (e.g. "Темная комната" instead of
# "Тёмная комната"). Folding them here means a search for either form
# matches both. Same for the upper-case pair (although ``str.lower``
# below makes that moot in practice). We could pull in `pyicu` for
# proper Russian collation, but a single ``str.translate`` is enough
# for the common case and keeps deps lean.
_RU_FOLD = str.maketrans({
    "ё": "е",
    "Ё": "е",
})


def _normalize_for_search(text: str) -> str:
    """Lower-case + ё→е fold so substring matching is forgiving of
    the common ё/е interchange in Russian titles."""
    return text.lower().translate(_RU_FOLD)


async def _ensure_tg(request: Request) -> Any | None:
    """Best-effort restore of the Telegram manager so /chats and /chats/refresh
    don't show 'not connected' just because the user landed here before
    visiting /auth/telegram after a server restart."""
    try:
        from src.api.routes_telegram import ensure_telegram_manager

        return await ensure_telegram_manager(request)
    except Exception as exc:  # noqa: BLE001
        log.debug("ensure_tg_failed", error=str(exc))
        return getattr(request.app.state, "telegram_manager", None)


async def _load_chats_with_state(
    session_factory: Any,
    *,
    type_filter: str | None = None,
    folder_id: int | None = None,
    recency_years: int | None = None,
    query: str = "",
    min_messages: int | None = None,
    sync_filter: str | None = None,
    syncing_now: set[int] | None = None,
) -> list[dict]:
    """Load chats from DB with optional filters applied in Python.

    type_filter: 'private'|'group'|'supergroup'|'channel'|'bot'|'public'|'all'
    folder_id: numeric Telegram folder id or None
    recency_years: keep only chats with last_message_date within N years
    query: substring match against title / @username
    min_messages: keep only chats whose `approx_message_count` is at
        least this number. Source: id of the latest message at
        list-fetch time — a useful proxy for how 'loaded' a dialog is
        (exact for DMs, monotonic-ish for channels even with deletes).
    sync_filter: one of ``all``/``synced``/``stale``/``never``/``syncing``
        — restricts the chat list to a specific sync lifecycle bucket.
        ``syncing`` requires the caller to pass ``syncing_now`` (the
        set of chat_ids with a mid-flight job in JobManager); empty
        otherwise.
    syncing_now: set of chat_ids currently being synced. Used both by
        the ``sync_filter='syncing'`` branch and for the per-row
        ``is_syncing`` flag in the returned dicts.
    """
    import json
    from datetime import datetime, timedelta

    if session_factory is None:
        return []
    async with session_factory() as s:
        rows = (await s.execute(select(Chat))).scalars().all()
        state_rows = (await s.execute(select(ChatState))).scalars().all()
    states = {sr.chat_id: sr for sr in state_rows}

    cutoff = None
    if recency_years and recency_years > 0:
        cutoff = datetime.now(UTC) - timedelta(days=int(recency_years) * 365)


    def _aware(dt):  # noqa: ANN001 — sqlite stores DateTime(tz=True) as naive
        if dt is None or dt.tzinfo is not None:
            return dt
        return dt.replace(tzinfo=UTC)

    needle = _normalize_for_search((query or "").strip().lower())

    out = []
    for c in rows:
        # Filter: type
        if type_filter and type_filter != "all":
            if type_filter == "public":
                if not c.is_public:
                    continue
            elif type_filter == "private":
                # 'private' means a personal chat (User, non-bot) that is not
                # publicly searchable. Match on type=PRIVATE here.
                if c.type.value != "private":
                    continue
            elif c.type.value != type_filter:
                continue
        # Filter: folder
        if folder_id is not None:
            try:
                fids = json.loads(c.folder_ids_json) if c.folder_ids_json else []
            except (TypeError, ValueError):
                fids = []
            if folder_id not in fids:
                continue
        # Filter: recency
        if cutoff is not None:
            lmd = _aware(c.last_message_date)
            if lmd is None or lmd < cutoff:
                continue
        # Filter: minimum approximate message count.
        if min_messages is not None and min_messages > 0:
            cnt = c.approx_message_count or 0
            if cnt < min_messages:
                continue
        # Filter: query (substring match across multiple fields).
        # Covers title, @username, first_name and last_name so a search
        # for 'Иванов' or 'ivanov' or part of a username all hit.
        # Normalises ё↔е and ъ↔ь — common Russian convention is to
        # write either form interchangeably (user typed "Тёмная" but
        # the channel title is "Темная" without the ё).
        if needle:
            haystack_parts = [
                (c.title or ""),
                (c.username or ""),
                (c.first_name or ""),
                (c.last_name or ""),
            ]
            hay = _normalize_for_search(" ".join(haystack_parts).lower())
            if needle not in hay:
                continue

        st = states.get(c.id)
        # Staleness: chat was exported before AND there's a fresher
        # message in Telegram than the last_export_at timestamp.
        # ``last_message_date`` updates on every refresh, while
        # ``last_export_at`` only moves forward when the user runs an
        # export — comparing the two is the most reliable "has new
        # content" signal without re-fetching the chat's tip id.
        last_export = _aware(st.last_export_at) if st and st.last_export_at else None
        last_msg = _aware(c.last_message_date)
        is_stale = bool(
            st is not None
            and st.last_exported_message_id is not None
            and last_export is not None
            and last_msg is not None
            and last_msg > last_export
        )
        out.append(
            {
                "id": c.id,
                "title": c.title,
                "username": c.username,
                "type": c.type.value,
                "is_public": c.is_public,
                "approx_message_count": c.approx_message_count,
                "avatar_path": c.avatar_path,
                "last_exported_message_id": st.last_exported_message_id if st else None,
                "last_export_at": st.last_export_at if st else None,
                "last_message_date": last_msg,
                "total_size_bytes": st.total_size_bytes if st else 0,
                "total_files": st.total_files if st else 0,
                "is_stale": is_stale,
                "auto_update": bool(st.watch_enabled) if st else False,
            }
        )
    # Sync-state pill filter. Applied AFTER the other filters so the
    # "Syncing now" / "Stale" / "Synced" / "Never" buckets are
    # computed against the user-visible subset, not the whole table.
    syncing_set = syncing_now or set()
    sf = (sync_filter or "all").strip().lower()
    if sf == "syncing":
        out = [r for r in out if r["id"] in syncing_set]
    elif sf == "stale":
        out = [r for r in out if r["is_stale"] and r["id"] not in syncing_set]
    elif sf == "synced":
        # Chat has been exported, isn't stale, and isn't being synced
        # right now — the green "up-to-date" bucket.
        out = [
            r for r in out
            if r["last_exported_message_id"] is not None
            and not r["is_stale"]
            and r["id"] not in syncing_set
        ]
    elif sf == "never":
        out = [r for r in out if r["last_exported_message_id"] is None]
    # ``all`` falls through unfiltered.

    out.sort(
        key=lambda r: (
            -(r["last_message_date"].timestamp() if r["last_message_date"] else 0),
            r["title"].lower(),
        )
    )
    return out


async def _load_folders(session_factory: Any) -> list[dict]:
    """Return folders sorted by chat_count desc."""
    if session_factory is None:
        return []
    from src.db.models import DialogFolderRow

    async with session_factory() as s:
        rows = (await s.execute(select(DialogFolderRow))).scalars().all()
    out = [
        {"id": r.id, "title": r.title, "chat_count": r.chat_count}
        for r in rows
    ]
    out.sort(key=lambda r: -r["chat_count"])
    return out


async def _total_chats(session_factory: Any | None) -> int:
    """Count rows in the Chat table without applying any filter."""
    if session_factory is None:
        return 0
    async with session_factory() as s:
        rows = (await s.execute(select(Chat))).scalars().all()
    return len(rows)


# Statuses we consider "mid-flight" for a sync job. Mirrors the same
# set in src/api/routes_jobs.py (``_ACTIVE_STATUSES``) so the chat-row
# "⟳ Идёт…" badge and the /jobs?status=active list agree on what's
# happening right now.
_ACTIVE_SYNC_STATUSES = frozenset(
    {"pending", "running", "exporting", "downloading", "deduping"}
)


def _active_syncing_chat_ids(request: Request) -> set[int]:
    """Return the set of chat_ids that currently have a mid-flight
    sync job. Empty set when the JobManager isn't attached yet
    (e.g. before login on a freshly booted server)."""
    mgr = getattr(request.app.state, "job_manager", None)
    if mgr is None:
        return set()
    out: set[int] = set()
    for job in mgr.list_jobs():
        st = getattr(getattr(job, "status", None), "value", None)
        if st not in _ACTIVE_SYNC_STATUSES:
            continue
        for cid in getattr(job.settings, "chat_ids", None) or ():
            try:
                out.add(int(cid))
            except (TypeError, ValueError):
                pass
    return out


def _active_sync_state(request: Request) -> dict[int, dict[str, Any]]:
    """Map chat_id → {job_id, status, percent, current, total} for
    every chat that currently has an active sync job.

    Used to render the per-chat ``⟳ Идёт N%`` button label with the
    server's freshest known progress, and to drive the client-side
    poller that keeps the label live. A multi-chat bulk-sync job
    will surface the same percent on every chat in its ``chat_ids``
    list — the runner emits one PROGRESS stream covering the whole
    pipeline, so we can't split it per-chat from the snapshot.
    """
    mgr = getattr(request.app.state, "job_manager", None)
    if mgr is None:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for job in mgr.list_jobs():
        st = getattr(getattr(job, "status", None), "value", None)
        if st not in _ACTIVE_SYNC_STATUSES:
            continue
        snapshot = {
            "job_id": job.id,
            "status": st,
            "percent": float(getattr(job, "percent", 0.0) or 0.0),
            "current": int(getattr(job, "current", 0) or 0),
            "total": int(getattr(job, "total", 0) or 0),
        }
        for cid in getattr(job.settings, "chat_ids", None) or ():
            try:
                # Older job overwrites are unlikely (one job per chat
                # under the new bulk-sync) but be deterministic — keep
                # whichever entry is HIGHER percent so a stale snapshot
                # doesn't overwrite a newer one.
                cid_i = int(cid)
            except (TypeError, ValueError):
                continue
            existing = out.get(cid_i)
            if existing is None or snapshot["percent"] >= existing["percent"]:
                out[cid_i] = snapshot
    return out


@router.get("", response_class=HTMLResponse)
async def list_chats(
    request: Request,
    session_factory: Any | None = Depends(_session_factory),
    type: str = "all",
    folder: str | None = None,
    recency: str | None = None,
    q: str = "",
    min_messages: str | None = None,
    sync: str = "all",
) -> HTMLResponse:
    tg = await _ensure_tg(request)
    folder_id = _coerce_int(folder)
    recency_years = _coerce_int(recency)
    min_msg = _coerce_int(min_messages)
    # Compute syncing_now once and reuse for both the sync-state
    # filter and the per-row ``sync_in_progress`` annotation below.
    syncing_now = _active_syncing_chat_ids(request)
    chats = await _load_chats_with_state(
        session_factory,
        type_filter=type,
        folder_id=folder_id,
        recency_years=recency_years,
        query=q,
        min_messages=min_msg,
        sync_filter=sync,
        syncing_now=syncing_now,
    )
    folders = await _load_folders(session_factory)
    total_count = await _total_chats(session_factory)
    last_refreshed = read_refresh_stamp()
    telegram_connected = bool(tg)
    # Count of chats that have been exported but have newer messages —
    # surfaced in the header so the bulk "Sync all stale" button shows
    # the number directly (and disables when there's nothing to do).
    # We compute against the UNFILTERED set so the button reflects the
    # full project state, not whatever search is active.
    stale_all = await _load_chats_with_state(session_factory)
    stale_count = sum(1 for c in stale_all if c.get("is_stale"))
    # Mark which chats have an actively running sync job so the row
    # shows "⟳ Идёт N%" instead of the misleading orange "Синхр." —
    # ``_advance_state`` only updates ``last_export_at`` at the end of
    # the dl+dedup pipeline, so a chat truly mid-flight is still
    # technically "stale" until then. We pass the per-chat job
    # snapshot (job_id, percent, …) into the template so the initial
    # render has the server's freshest known progress; the JS poller
    # then keeps it ticking without a page reload.
    sync_state = _active_sync_state(request)
    for c in chats:
        snap = sync_state.get(c["id"])
        c["sync_in_progress"] = snap is not None
        c["sync_percent"] = float(snap["percent"]) if snap else 0.0
        c["sync_job_id"] = snap["job_id"] if snap else None

    # Counts for the sync-state filter pills. We compute against the
    # UNFILTERED universe (same approach as ``stale_count`` above) so
    # the badge numbers don't shrink to zero when the user narrows
    # by type / folder / search query — they always reflect the
    # project-wide breakdown.
    all_chats = await _load_chats_with_state(session_factory, syncing_now=syncing_now)
    sync_counts: dict[str, int] = {
        "all": len(all_chats),
        "synced": 0,
        "stale": 0,
        "never": 0,
        "syncing": 0,
    }
    for c_ in all_chats:
        if c_["id"] in syncing_now:
            sync_counts["syncing"] += 1
        elif c_["last_exported_message_id"] is None:
            sync_counts["never"] += 1
        elif c_["is_stale"]:
            sync_counts["stale"] += 1
        else:
            sync_counts["synced"] += 1
    sync_filter = (sync or "all").strip().lower()
    if sync_filter not in sync_counts:
        sync_filter = "all"

    return _templates(request).TemplateResponse(
        request,
        "chats.html",
        {
            "title": "Чаты",
            "chats": chats,
            "folders": folders,
            "telegram_connected": telegram_connected,
            "query": q,
            "active_type": type,
            "active_folder": folder,
            "active_recency": recency,
            "active_min_messages": min_messages,
            "active_sync": sync_filter,
            "sync_counts": sync_counts,
            "filtered_count": len(chats),
            "total_count": total_count,
            "stale_count": stale_count,
            "last_refreshed_iso": last_refreshed.isoformat() if last_refreshed else None,
            "last_refreshed_human": humanise_refreshed(last_refreshed),
        },
    )


@router.get("/fragment", response_class=HTMLResponse)
async def list_chats_fragment(
    request: Request,
    q: str = "",
    type: str = "all",
    folder: str | None = None,
    recency: str | None = None,
    min_messages: str | None = None,
    sync: str = "all",
    session_factory: Any | None = Depends(_session_factory),
) -> HTMLResponse:
    """HTMX live-search / filter endpoint. Returns the inner grid only.

    `folder` / `recency` / `min_messages` come in as `str | None` so an
    empty form value `…=` doesn't blow up FastAPI's body validator with
    422; we coerce to int ourselves and treat blanks as None.
    """
    folder_id = _coerce_int(folder)
    recency_years = _coerce_int(recency)
    min_msg = _coerce_int(min_messages)
    syncing_now = _active_syncing_chat_ids(request)
    chats = await _load_chats_with_state(
        session_factory,
        type_filter=type,
        folder_id=folder_id,
        recency_years=recency_years,
        query=q,
        min_messages=min_msg,
        sync_filter=sync,
        syncing_now=syncing_now,
    )
    total_count = await _total_chats(session_factory)
    # Same as in /chats — stale count is project-wide, not filtered.
    stale_all = await _load_chats_with_state(session_factory, syncing_now=syncing_now)
    stale_count = sum(1 for c in stale_all if c.get("is_stale"))
    # Per-bucket counts for the sync-state pill row — computed against
    # the SAME unfiltered universe as in /chats so the badge numbers
    # stay consistent between full-page renders and HTMX swaps.
    sync_counts: dict[str, int] = {
        "all": len(stale_all),
        "synced": 0,
        "stale": 0,
        "never": 0,
        "syncing": 0,
    }
    for c_ in stale_all:
        if c_["id"] in syncing_now:
            sync_counts["syncing"] += 1
        elif c_["last_exported_message_id"] is None:
            sync_counts["never"] += 1
        elif c_["is_stale"]:
            sync_counts["stale"] += 1
        else:
            sync_counts["synced"] += 1
    # Annotate active-sync state per chat (see comment in /chats route).
    sync_state = _active_sync_state(request)
    for c in chats:
        snap = sync_state.get(c["id"])
        c["sync_in_progress"] = snap is not None
        c["sync_percent"] = float(snap["percent"]) if snap else 0.0
        c["sync_job_id"] = snap["job_id"] if snap else None
    return _templates(request).TemplateResponse(
        request,
        "_chats_fragment.html",
        {
            "chats": chats,
            "query": q,
            "filtered_count": len(chats),
            "total_count": total_count,
            "stale_count": stale_count,
            "sync_counts": sync_counts,
            "is_oob": True,
        },
    )


@router.post("/refresh", response_class=HTMLResponse)
async def refresh_chats(
    request: Request,
    session_factory: Any | None = Depends(_session_factory),
) -> HTMLResponse:
    """Re-fetch dialogs from Telegram and update local mirror."""
    tg = await _ensure_tg(request)
    if tg is None or session_factory is None:
        return _templates(request).TemplateResponse(
            request,
            "_chats_fragment.html",
            {"chats": [], "query": "", "error": "Telegram not connected yet"},
        )

    import json

    from src.db.models import DialogFolderRow

    fetched = 0
    by_type: dict[str, int] = {}

    # Pull folders first so each chat record can carry its folder ids.
    try:
        folders = await tg.list_folders()
    except Exception as exc:  # noqa: BLE001
        log.warning("list_folders_failed", error=str(exc))
        folders = []

    try:
        async with session_factory() as s:
            # Refresh folders table.
            seen_folder_ids: set[int] = set()
            for f in folders:
                seen_folder_ids.add(f.id)
                row = (
                    await s.execute(
                        select(DialogFolderRow).where(DialogFolderRow.id == f.id)
                    )
                ).scalar_one_or_none()
                if row is None:
                    s.add(
                        DialogFolderRow(
                            id=f.id, title=f.title, chat_count=len(f.chat_ids)
                        )
                    )
                else:
                    row.title = f.title
                    row.chat_count = len(f.chat_ids)
            # Drop folders that no longer exist on the user's TG account.
            stale = (await s.execute(select(DialogFolderRow))).scalars().all()
            for r in stale:
                if r.id not in seen_folder_ids:
                    await s.delete(r)

            async for d in tg.iter_dialogs():
                by_type[d.type.value] = by_type.get(d.type.value, 0) + 1
                folders_json = json.dumps(d.folder_ids) if d.folder_ids else None
                existing = (
                    await s.execute(select(Chat).where(Chat.id == d.id))
                ).scalar_one_or_none()
                if existing:
                    existing.title = d.title
                    existing.username = d.username
                    existing.type = d.type
                    existing.is_archived = d.is_archived
                    existing.is_public = d.is_public
                    existing.first_name = d.first_name
                    existing.last_name = d.last_name
                    existing.folder_ids_json = folders_json
                    if d.last_message_date is not None:
                        existing.last_message_date = _as_datetime(d.last_message_date)
                    if d.approx_message_count is not None:
                        existing.approx_message_count = d.approx_message_count
                else:
                    s.add(
                        Chat(
                            id=d.id,
                            title=d.title,
                            username=d.username,
                            type=d.type,
                            is_archived=d.is_archived,
                            is_public=d.is_public,
                            first_name=d.first_name,
                            last_name=d.last_name,
                            folder_ids_json=folders_json,
                            last_message_date=_as_datetime(d.last_message_date),
                            approx_message_count=d.approx_message_count,
                        )
                    )
                fetched += 1
            await s.commit()
        log.info(
            "chats_iter_dialogs_done",
            total=fetched,
            by_type=by_type,
            folder_count=len(folders),
        )

        # Background avatar download — fire-and-forget. UI shows initials
        # placeholders until files appear.
        try:
            import asyncio

            asyncio.create_task(
                _download_avatars_bg(tg, session_factory),
                name="avatar-download",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("avatar_bg_spawn_failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface error to UI
        log.exception("chats_refresh_failed", error=str(exc))
        return _templates(request).TemplateResponse(
            request,
            "_chats_fragment.html",
            {"chats": [], "query": "", "error": str(exc)},
        )

    log.info("chats_refreshed", count=fetched)
    refreshed_iso = write_refresh_stamp()
    chats = await _load_chats_with_state(session_factory)
    total_count = await _total_chats(session_factory)
    refreshed_dt = read_refresh_stamp()
    return _templates(request).TemplateResponse(
        request,
        "_chats_fragment.html",
        {
            "chats": chats,
            "query": "",
            "refreshed": fetched,
            "filtered_count": len(chats),
            "total_count": total_count,
            "last_refreshed_iso": refreshed_iso,
            "last_refreshed_human": humanise_refreshed(refreshed_dt),
            "is_oob": True,
        },
    )


async def _download_avatars_bg(tg: Any, session_factory: Any) -> None:
    """Walk all chats with no avatar yet and download a profile photo.

    Telethon's download_profile_photo writes a JPEG. We save under
    settings.avatars_dir / "{chat_id}.jpg" — that's what /avatars/{id}
    serves. Errors are swallowed; a missing avatar shows initials.
    """
    from src.config import get_settings

    s = get_settings()
    s.avatars_dir.mkdir(parents=True, exist_ok=True)

    async with session_factory() as session:
        rows = (await session.execute(select(Chat))).scalars().all()
        chat_ids = [c.id for c in rows]

    for chat_id in chat_ids:
        dest = s.avatars_dir / f"{chat_id}.jpg"
        if dest.exists():
            continue
        try:
            path = await tg.download_avatar(chat_id, dest)
            if path is None:
                # Touch a 0-byte placeholder so we don't keep retrying.
                dest.write_bytes(b"")
        except Exception as exc:  # noqa: BLE001
            log.debug("avatar_download_skip", chat_id=chat_id, error=str(exc))


def _as_datetime(obj: Any) -> datetime | None:
    """Telethon yields tz-aware datetimes already; coerce just in case."""
    if obj is None:
        return None
    if isinstance(obj, datetime):
        return obj
    return None


# ---------------------------------------------------------------------------
# Quick-sync endpoints — incremental re-export with sensible defaults.
#
# The export-settings modal is great for first-time exports where the user
# wants fine control (which media types, date range, threads). But for the
# common "I just want to pick up where I left off" flow, the modal is
# overkill. These two POST endpoints submit a Job to the same JobManager
# with ``only_new=True`` and ALL media types pre-selected.
# ---------------------------------------------------------------------------


def _job_manager(request: Request) -> Any | None:
    return getattr(request.app.state, "job_manager", None)


def _build_quick_sync_settings(chat_ids: list[int]) -> Any:
    """Construct a JobSettings with ``only_new=True`` + all media types.

    Local import keeps the route module import-graph minimal.
    """
    from src.jobs.models import DEFAULT_MEDIA_TYPES, JobSettings

    return JobSettings(
        chat_ids=list(chat_ids),
        media_types=list(DEFAULT_MEDIA_TYPES),
        only_new=True,
        dedup=True,
        with_content=True,
        threads_per_file=4,
        parallel_tasks=2,
    )


@router.post("/{chat_id}/sync")
async def sync_one_chat(
    chat_id: int,
    request: Request,
    session_factory: Any | None = Depends(_session_factory),
    mgr: Any | None = Depends(_job_manager),
):  # noqa: ANN201
    """Kick off an incremental export for a single chat.

    Uses the chat's ``last_exported_message_id`` as the resume watermark
    (handled inside ``ExportRunner._run_chat`` via ``only_new=True``).
    Returns ``{"job_id": "...", "status": "..."}`` so the UI can redirect
    to /jobs/{id} without re-fetching state.
    """
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    if mgr is None:
        raise HTTPException(503, "Job manager not available")
    if session_factory is None:
        raise HTTPException(503, "Session factory not ready")

    # Confirm the chat exists — otherwise the job would silently fail
    # later. Better to surface 404 here so the UI can show a clear
    # message instead of a vague error inside /jobs.
    async with session_factory() as s:
        row = (
            await s.execute(select(Chat).where(Chat.id == chat_id))
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"chat {chat_id} not found — refresh the chat list first")

    settings = _build_quick_sync_settings([chat_id])
    try:
        job = await mgr.submit(settings)
    except Exception as exc:  # noqa: BLE001
        log.exception("sync_one_chat_submit_failed", chat_id=chat_id, error=str(exc))
        return JSONResponse(
            {"detail": f"Не удалось создать задачу: {type(exc).__name__}: {exc}"},
            status_code=500,
        )
    return JSONResponse({"job_id": job.id, "status": job.status.value})


@router.post("/{chat_id}/auto-update")
async def toggle_auto_update(
    chat_id: int,
    request: Request,
    session_factory: Any | None = Depends(_session_factory),
):  # noqa: ANN201
    """Enable / disable the per-chat auto-update (watch) flag.

    Request body (JSON): ``{"enabled": true|false}``.
    If the body is absent or unparseable the current value is TOGGLED.

    Returns ``{"chat_id": <id>, "auto_update": <new bool>}``.
    HTTP 404 when the chat is not in the local DB.
    """
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from src.db.session import transaction as _tx

    if session_factory is None:
        raise HTTPException(503, "Session factory not ready")

    # Parse the request body — missing / empty body means "toggle".
    desired: bool | None = None
    try:
        body = await request.json()
        if isinstance(body, dict) and "enabled" in body:
            desired = bool(body["enabled"])
    except Exception:  # noqa: BLE001
        pass  # treat as toggle

    async with session_factory() as s, _tx(s):
        chat_row = (
            await s.execute(select(Chat).where(Chat.id == chat_id))
        ).scalar_one_or_none()
        if chat_row is None:
            raise HTTPException(404, f"chat {chat_id} not found — refresh the chat list first")

        state_row = (
            await s.execute(select(ChatState).where(ChatState.chat_id == chat_id))
        ).scalar_one_or_none()
        if state_row is None:
            current = False
            new_value = desired if desired is not None else True
            state_row = ChatState(chat_id=chat_id, watch_enabled=new_value)
            s.add(state_row)
        else:
            current = bool(state_row.watch_enabled)
            new_value = desired if desired is not None else (not current)
            state_row.watch_enabled = new_value

    log.info(
        "auto_update_toggled",
        chat_id=chat_id,
        old=current,
        new=new_value,
    )
    return JSONResponse({"chat_id": chat_id, "auto_update": new_value})


@router.post("/sync-stale")
async def sync_all_stale(
    request: Request,
    session_factory: Any | None = Depends(_session_factory),
    mgr: Any | None = Depends(_job_manager),
):  # noqa: ANN201
    """Bulk-sync every chat where ``last_message_date > last_export_at``.

    Spawns ONE JOB PER STALE CHAT so each gets its own row in /jobs with
    its own title, progress bar, and Cancel button. tdl's bolt-DB is
    single-writer so the jobs queue up behind the lock anyway — there's
    no parallelism lost, only visibility gained. Previously this used
    to bundle every stale chat into a single mega-job, which showed
    only the first chat's title and a single "0/0 exporting" bar that
    sat frozen for many minutes while tdl walked the first chat — the
    user reasonably concluded "ничего не работает".

    Returns ``{"job_ids": [...], "chat_ids": [...], "count": N}`` on
    success, or 204 No Content when nothing is stale.
    """
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse, Response

    if mgr is None:
        raise HTTPException(503, "Job manager not available")
    if session_factory is None:
        raise HTTPException(503, "Session factory not ready")

    chats = await _load_chats_with_state(session_factory)
    stale_ids = [c["id"] for c in chats if c.get("is_stale")]
    if not stale_ids:
        return Response(status_code=204)

    job_ids: list[str] = []
    failures: list[dict[str, Any]] = []
    for cid in stale_ids:
        settings = _build_quick_sync_settings([cid])
        try:
            job = await mgr.submit(settings)
        except Exception as exc:  # noqa: BLE001
            log.exception("sync_stale_submit_failed", chat_id=cid, error=str(exc))
            failures.append({"chat_id": cid, "error": f"{type(exc).__name__}: {exc}"})
            continue
        job_ids.append(job.id)

    if not job_ids:
        # Every submission failed — surface the first error so the UI shows
        # something useful instead of a silent empty array.
        detail = failures[0]["error"] if failures else "Unknown error"
        return JSONResponse({"detail": detail, "failures": failures}, status_code=500)

    log.info(
        "sync_stale_submitted",
        job_ids=job_ids,
        count=len(job_ids),
        failed=len(failures),
    )
    payload: dict[str, Any] = {
        "job_ids": job_ids,
        "chat_ids": stale_ids,
        "count": len(job_ids),
    }
    if failures:
        payload["failures"] = failures
    return JSONResponse(payload)


def _resolve_chat_export_dir(export_dir, chat_id: int, title: str | None):
    """Find the on-disk export folder for ``chat_id``.

    Mirrors ``src.jobs.exporter._chat_slug`` so we land on the same
    directory the exporter wrote into. Falls back to globbing by the
    trailing ``_<id>`` suffix when the title we have now no longer
    matches the title at export time. Returns ``Path | None`` —
    untyped here to avoid an extra ``from pathlib import Path`` at
    module top since this module already keeps that local-imported.
    """
    from src.jobs.exporter import _chat_slug  # local import — heavy module

    candidate = export_dir / _chat_slug(chat_id, title)
    if candidate.is_dir():
        return candidate
    # Title drift: search for any folder ending in _<chat_id>.
    for entry in export_dir.glob(f"chat_*_{chat_id}"):
        if entry.is_dir():
            return entry
    legacy = export_dir / f"chat_{chat_id}"
    if legacy.is_dir():
        return legacy
    return None


@router.post("/{chat_id}/delete")
async def delete_chat(
    chat_id: int,
    request: Request,
    session_factory: Any | None = Depends(_session_factory),
):  # noqa: ANN201
    """Wipe a chat's local artifacts.

    Removes the ``chats`` row (CASCADE → ``chat_states`` + the
    chat's ``export_jobs`` audit rows) and, if present, the
    ``exports/chat_<slug>_<id>/`` folder with all downloaded media.
    ``media_pool`` (dedup-hardlinked content addressed by SHA-256)
    is NOT touched — files there may still be referenced by other
    chats. The Telegram dialog itself is not affected; the chat
    will reappear on the next "Refresh chats" call.

    Refuses (409) when there's a mid-flight sync job for this chat
    so the user doesn't yank a folder out from under a running tdl.
    The UI hides the delete button in that case but the route is
    defensive anyway.
    """
    import shutil
    from pathlib import Path

    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from src.config import get_settings

    if session_factory is None:
        raise HTTPException(503, "Session factory not ready")

    # Block deletion while a sync is in flight — otherwise we'd
    # be deleting a chat folder whose tdl child is still writing to it.
    if chat_id in _active_syncing_chat_ids(request):
        raise HTTPException(
            409,
            f"Чат {chat_id} сейчас синхронизируется — подожди или отмени задачу",
        )

    settings = get_settings()
    async with session_factory() as s:
        row = (
            await s.execute(select(Chat).where(Chat.id == chat_id))
        ).scalar_one_or_none()
        if row is None:
            # Idempotent — already gone is a successful no-op so the
            # UI can fire the request without worrying about races.
            return JSONResponse(
                {"deleted": False, "removed_files": 0, "freed_bytes": 0},
                status_code=200,
            )
        title = row.title

    # Remove the export folder BEFORE the DB row so a half-cleaned
    # state is "chat still in DB but folder missing" — recoverable
    # via "Refresh chats" rather than "orphan folder with no DB row".
    chat_dir = _resolve_chat_export_dir(settings.export_dir, chat_id, title)
    removed_files = 0
    freed_bytes = 0
    if chat_dir is not None and chat_dir.is_dir():
        # Count first so the response can show the user what they
        # erased — handy when the click was a typo + Ctrl+Z impulse.
        for p in chat_dir.rglob("*"):
            if p.is_file():
                try:
                    freed_bytes += p.stat().st_size
                except OSError:
                    pass
                removed_files += 1
        try:
            shutil.rmtree(chat_dir, ignore_errors=False)
        except OSError as exc:
            log.exception("delete_chat_rmtree_failed", chat_id=chat_id, error=str(exc))
            raise HTTPException(
                500,
                f"Не удалось удалить папку {chat_dir}: {exc}",
            ) from exc

    from sqlalchemy import delete as _delete

    from src.db.models import ExportJob
    from src.db.session import transaction as _tx

    # NOTE on cascade: ``ChatState`` and ``ExportJob`` both declare
    # ``ondelete="CASCADE"`` on their ``chat_id`` FK, but SQLite only
    # enforces foreign-key constraints when ``PRAGMA foreign_keys=ON``
    # is set per-connection — which we don't currently do. To stay
    # robust regardless of the PRAGMA state, the route deletes the
    # children explicitly inside the same transaction. If/when we
    # decide to flip the pragma globally these statements turn into
    # cheap no-ops.
    async with session_factory() as s, _tx(s):
        await s.execute(_delete(ChatState).where(ChatState.chat_id == chat_id))
        await s.execute(_delete(ExportJob).where(ExportJob.chat_id == chat_id))
        row = (
            await s.execute(select(Chat).where(Chat.id == chat_id))
        ).scalar_one_or_none()
        if row is not None:
            await s.delete(row)

    log.info(
        "chat_deleted",
        chat_id=chat_id,
        title=title,
        chat_dir=str(chat_dir) if chat_dir else None,
        removed_files=removed_files,
        freed_bytes=freed_bytes,
    )
    return JSONResponse(
        {
            "deleted": True,
            "title": title,
            "chat_dir": str(chat_dir) if chat_dir else None,
            "removed_files": removed_files,
            "freed_bytes": freed_bytes,
        },
        status_code=200,
    )


@router.get("/{chat_id}/sync-status")
async def chat_sync_status(
    chat_id: int,
    request: Request,
):  # noqa: ANN201
    """Return the current sync state for a chat.

    Shape::

        {"in_progress": bool, "status": "running"|"succeeded"|...,
         "percent": 0.0-100.0, "current": int, "total": int,
         "job_id": str|null}

    Drives the JS poller that ticks every few seconds and updates the
    "⟳ Идёт N%" label without a full page reload. When the most
    recent job for this chat is in a terminal state, the JS reloads
    /chats so the row swaps back to the "fresh" / "stale" rendering.

    We prefer the live JobManager snapshot (in-memory percent, current,
    total) over the persisted DB row — the DB only has the final
    counters and would otherwise show 0% for an in-flight run.
    """
    from fastapi.responses import JSONResponse

    mgr = getattr(request.app.state, "job_manager", None)
    if mgr is None:
        return JSONResponse({"in_progress": False, "status": "idle"}, status_code=200)

    # Find the newest job touching this chat — multiple historical
    # jobs for the same chat are common (retries, manual reruns),
    # but only the most recent one matters for "current state".
    # SQLite-backed restores return ``started_at`` as TZ-naive even
    # though the column is ``DateTime(timezone=True)``, while jobs
    # spawned in this server lifetime are TZ-aware (``datetime.now(UTC)``).
    # Comparing the two raises TypeError, so we coerce both into
    # epoch seconds first.
    from datetime import UTC as _UTC

    def _ts(dt) -> float:  # noqa: ANN001
        if dt is None:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        return dt.timestamp()

    best = None
    best_started_ts: float = -1.0
    for job in mgr.list_jobs():
        chats = getattr(job.settings, "chat_ids", None) or ()
        if chat_id not in chats:
            continue
        started = getattr(job, "started_at", None) or getattr(job, "created_at", None)
        started_ts = _ts(started)
        if best is None or started_ts > best_started_ts:
            best = job
            best_started_ts = started_ts
    if best is None:
        return JSONResponse({"in_progress": False, "status": "idle"}, status_code=200)
    st = getattr(getattr(best, "status", None), "value", "")
    return JSONResponse(
        {
            "in_progress": st in _ACTIVE_SYNC_STATUSES,
            "status": st,
            "percent": float(getattr(best, "percent", 0.0) or 0.0),
            "current": int(getattr(best, "current", 0) or 0),
            "total": int(getattr(best, "total", 0) or 0),
            "job_id": best.id,
        },
        status_code=200,
    )


