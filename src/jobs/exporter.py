"""The export runner that glues tdl + state + dedup together.

High-level flow for each chat in `JobSettings.chat_ids`:

1. Load `ChatState` for the chat.
2. Decide the message-id range to export:
   * ``recent_messages`` set  ⇒ one ``-T last -i N`` window (no chunking).
   * otherwise               ⇒ id-range sweep from the resume point
     (``export_cursor_message_id`` + 1, else the ``only_new`` watermark + 1,
     else 0) up to the chat tip, split into ``_EXPORT_CHUNK_ID_SPAN`` windows.
3. For each window: ``tdl chat export -i lo,hi -T id`` → filter → manifest →
   ``tdl dl`` → merge messages.json. Persist ``export_cursor_message_id`` after
   every completed window so a crash resumes mid-sweep instead of restarting.
4. Once all windows are done: dedup the media dir, advance the watermark to the
   swept tip + clear the cursor, enrich + render the HTML transcript.

Why chunk: a single full-history ``tdl chat export`` of a large chat can run
for 10+ minutes and is NOT resumable — one ``context deadline exceeded`` loses
the whole pass. id-range windows seek efficiently server-side (verified: a
window near the tip is as fast as one near the start), so each call is bounded
and checkpointed.

Errors:
- FloodWait from tdl ⇒ emit warning update, sleep the countdown, retry.
- Transient network/timeout ⇒ bounded exponential-backoff retry.
- Any permanent failure ⇒ raise so the JobManager marks the job FAILED.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from src.db.models import Chat, ChatState
from src.db.session import SessionFactory, transaction
from src.jobs.history import (
    persist_message_history as _persist_message_history,
)
from src.jobs.history import (
    render_history_html as _render_history_html,
)
from src.jobs.job_manager import Job, JobManager
from src.jobs.models import JobSettings, JobUpdate, JobUpdateKind, MediaType
from src.logging_setup import get_logger
from src.services.deduplicator import DedupBatchStats, Deduplicator
from src.services.tdl_retry import FloodWaitNotRetried, with_retry

log = get_logger(__name__)


# Mapping from our UI MediaType enum to the tdl message media discriminators
# present in `tdl chat export` JSON output. tdl emits messages with a `media`
# object whose `class` / `type` field identifies the category. We cover the
# shapes we've seen across recent tdl releases.
# tdl chat export emits messages with these CamelCase keys:
#   ID:int, Date:int, Message:str, Media.Name:str, Media.Size:int64, Media.DC:int
# There is NO explicit media-type field — we infer it from the file
# extension of Media.Name. Map UI MediaType → file-extension whitelist.
_TDL_MEDIA_EXTS: dict[MediaType, tuple[str, ...]] = {
    MediaType.PHOTO:      ("jpg", "jpeg", "png", "webp", "heic", "heif"),
    MediaType.VIDEO:      ("mp4", "mov", "mkv", "webm", "avi", "m4v"),
    MediaType.VOICE:      ("ogg", "oga", "opus"),
    MediaType.VIDEO_NOTE: ("mp4",),  # heuristic — round videos same ext
    MediaType.AUDIO:      ("mp3", "m4a", "flac", "wav", "aac", "oga"),
    MediaType.DOCUMENT:   ("pdf", "doc", "docx", "xls", "xlsx", "zip", "rar",
                          "7z", "txt", "csv", "tsv", "json", "ppt", "pptx",
                          "epub", "odt"),
    MediaType.STICKER:    ("webp", "tgs"),
    MediaType.GIF:        ("gif", "mp4"),  # Telegram GIFs ride mp4
}


# Default message-id span per chunked-export window. tdl seeks an id range
# server-side, so a window bounds each `tdl chat export` to a completable size:
# a full-history export of a 44k-message chat took ~13 min as one timeout-prone
# call; chunked into ~20k-id windows it becomes ~24 calls of ~30 s each, every
# one checkpointed (export_cursor_message_id) and therefore resumable.
_EXPORT_CHUNK_ID_SPAN = 20_000


@dataclass(slots=True)
class ChatRunResult:
    chat_id: int
    exported_last_id: int | None
    files_downloaded: int
    bytes_downloaded: int
    dedup_stats: DedupBatchStats


@dataclass(slots=True)
class _WindowResult:
    """Per-window outcome accumulated by the chunked-export orchestrator."""

    kept: int
    max_id: int | None
    files: int
    bytes: int
    resolved_handle: str | int | None


class ExportRunner:
    """Callable suitable for `JobManager(runner=ExportRunner(...))`."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        tdl_wrapper,  # type: ignore[no-untyped-def] - duck-typed across agent boundaries
        deduplicator: Deduplicator,
        export_dir: Path,
        avatars_dir: Path | None = None,
        telegram_manager_provider=None,  # noqa: ANN001 — duck-typed
    ) -> None:
        self._sf = session_factory
        self._tdl = tdl_wrapper
        self._dedup = deduplicator
        self._export_dir = export_dir
        self._avatars_dir = avatars_dir
        # Late-bound: composition stores a lambda that reads
        # ``app.state.telegram_manager`` so a re-attached client after
        # /auth/telegram is picked up without restarting jobs.
        self._tg_provider = telegram_manager_provider

    async def __call__(self, job: Job, manager: JobManager) -> None:
        settings = job.settings
        # Decide whether we even need to prime tdl's bolt-DB peer cache.
        # We can skip the (potentially slow) `chat ls` if every chat in
        # the job has a @username we can pass directly — tdl resolves
        # @usernames server-side without consulting the local cache.
        need_prime = await self._need_warmup(settings.chat_ids)
        if need_prime:
            await self._prime_tdl_cache(job, manager)
        for chat_id in settings.chat_ids:
            await self._run_chat(chat_id, settings, job, manager)

    async def _need_warmup(self, chat_ids: list[int]) -> bool:
        """True if at least one chat in the job has no @username — we'll
        pass its numeric id and need a primed bolt cache for tdl to resolve."""
        async with self._sf() as s:
            for cid in chat_ids:
                row = (
                    await s.execute(select(Chat).where(Chat.id == cid))
                ).scalar_one_or_none()
                if row is None or not row.username:
                    return True
        return False

    async def _prime_tdl_cache(self, job: Job, manager: JobManager) -> None:
        try:
            await self._tdl.chat_ls()
            log.info("tdl_cache_primed")
        except Exception as exc:  # noqa: BLE001 — non-fatal; chat_export may still work
            log.warning("tdl_chat_ls_failed", error=str(exc))
            await manager.emit(
                job,
                JobUpdate(
                    kind=JobUpdateKind.LOG,
                    ts=datetime.now(UTC),
                    job_id=job.id,
                    level="warn",
                    message=f"tdl chat ls warmup failed (continuing anyway): {exc}",
                ),
            )

    async def _run_chat(
        self,
        chat_id: int,
        settings: JobSettings,
        job: Job,
        manager: JobManager,
    ) -> None:
        await manager.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.STATUS,
                ts=datetime.now(UTC),
                job_id=job.id,
                chat_id=chat_id,
                status="exporting",
            ),
        )

        state = await self._load_state(chat_id)

        # Prefer @username when available — tdl resolves it server-side and
        # the call works even if the bolt cache is empty for this peer.
        chat_handle: str | int = await self._chat_handle_for(chat_id)
        candidates = _tdl_chat_handle_candidates(chat_handle)

        # Loud breadcrumb — both to server.log and the live job stream so the
        # user can tell exactly which window we are pulling, and which handle
        # tdl will be called with.
        watermark = state.last_exported_message_id if state else None
        cursor = state.export_cursor_message_id if state else None
        log.info(
            "exporter_chat_start",
            job_id=job.id,
            chat_id=chat_id,
            chat_handle=str(chat_handle),
            only_new=settings.only_new,
            last_exported_message_id=watermark,
            export_cursor=cursor,
            recent_messages=settings.recent_messages,
            with_content=settings.with_content,
            date_from=settings.date_from,
            date_to=settings.date_to,
            media_types=[mt.value for mt in settings.media_types],
        )
        await manager.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.LOG,
                ts=datetime.now(UTC),
                job_id=job.id,
                chat_id=chat_id,
                message=(
                    f"start: handle={chat_handle} only_new={settings.only_new} "
                    f"watermark={watermark} cursor={cursor} "
                    f"recent={settings.recent_messages} window=[{settings.date_from}..{settings.date_to}] "
                    f"media={[mt.value for mt in settings.media_types]}"
                ),
                level="info",
            ),
        )

        # Resolve the export directory once — every window downloads into the
        # same media dir and merges into the same messages.json.
        title = await self._chat_title_for(chat_id)
        chat_dir = self._export_dir / _chat_slug(chat_id, title)
        # Migrate from the legacy plain-id folder if it exists.
        legacy = self._export_dir / f"chat_{chat_id}"
        if legacy.exists() and legacy != chat_dir and not chat_dir.exists():
            try:
                legacy.rename(chat_dir)
                log.info("export_dir_migrated", from_=str(legacy), to=str(chat_dir))
            except OSError as exc:
                log.warning("export_dir_migrate_failed", error=str(exc))
        chat_out = chat_dir / "media"
        chat_out.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="tg_export_") as tmp:
            tmp_dir = Path(tmp)

            # ---- decide the windows to export ----
            if settings.recent_messages:
                # Bounded "last N" request: a single window, no id-range / no
                # cursor — the request is already small.
                windows: list[tuple[str, int | None, int | None, int | None]] = [
                    ("recent", None, None, settings.recent_messages)
                ]
                sweep_tip: int | None = None
            else:
                lo = _chunk_start(state, settings)
                sweep_tip = await self._probe_tip(
                    chat_id=chat_id,
                    candidates=candidates,
                    settings=settings,
                    tmp_dir=tmp_dir,
                    job=job,
                    manager=manager,
                )
                if sweep_tip is None:
                    await manager.emit(
                        job,
                        JobUpdate(
                            kind=JobUpdateKind.LOG,
                            ts=datetime.now(UTC),
                            job_id=job.id,
                            chat_id=chat_id,
                            message="чат пуст или вершина не определена — экспортировать нечего",
                            level="warn",
                        ),
                    )
                    return
                if lo > sweep_tip:
                    await manager.emit(
                        job,
                        JobUpdate(
                            kind=JobUpdateKind.LOG,
                            ts=datetime.now(UTC),
                            job_id=job.id,
                            chat_id=chat_id,
                            message="нет новых сообщений (вы уже на вершине чата)",
                            level="info",
                        ),
                    )
                    # Clear any stale in-progress cursor; keep the watermark.
                    if state is not None and state.export_cursor_message_id is not None:
                        await self._advance_state(
                            chat_id, sweep_tip, files=0, bytes_=0, clear_cursor=True
                        )
                    return
                windows = [
                    (
                        str(w),
                        w,
                        min(w + _EXPORT_CHUNK_ID_SPAN - 1, sweep_tip),
                        None,
                    )
                    for w in range(lo, sweep_tip + 1, _EXPORT_CHUNK_ID_SPAN)
                ]
                if len(windows) > 1:
                    await manager.emit(
                        job,
                        JobUpdate(
                            kind=JobUpdateKind.LOG,
                            ts=datetime.now(UTC),
                            job_id=job.id,
                            chat_id=chat_id,
                            message=(
                                f"большой диапазон [{lo}..{sweep_tip}] — экспорт "
                                f"{len(windows)} окнами по {_EXPORT_CHUNK_ID_SPAN} id "
                                f"(каждое окно резюмируемо)"
                            ),
                            level="info",
                        ),
                    )

            # ---- per-window export + download ----
            resolved: str | int | None = None
            total_kept = 0
            total_files = 0
            total_bytes = 0
            overall_max_id: int | None = None
            n_windows = len(windows)
            for idx, (label, w_lo, w_hi, last_n) in enumerate(windows, 1):
                res = await self._export_one_window(
                    chat_id=chat_id,
                    # Once a candidate format resolves, pin it for the rest of
                    # the sweep so later windows skip the empty-peer fallback.
                    candidates=[resolved] if resolved is not None else candidates,
                    settings=settings,
                    from_id=w_lo,
                    to_id=w_hi,
                    last_n=last_n,
                    tmp_dir=tmp_dir,
                    chat_dir=chat_dir,
                    chat_out=chat_out,
                    title=title,
                    job=job,
                    manager=manager,
                    window_index=idx,
                    window_total=n_windows,
                    window_label=label,
                )
                if res.resolved_handle is not None:
                    resolved = res.resolved_handle
                total_kept += res.kept
                total_files += res.files
                total_bytes += res.bytes
                if res.max_id is not None:
                    overall_max_id = (
                        res.max_id
                        if overall_max_id is None
                        else max(overall_max_id, res.max_id)
                    )
                # Durable resume point after each completed window (chunked mode).
                if w_hi is not None:
                    await self._advance_chunk_cursor(chat_id, w_hi)

            # ---- once-only post-processing ----
            if total_kept == 0:
                await manager.emit(
                    job,
                    JobUpdate(
                        kind=JobUpdateKind.LOG,
                        ts=datetime.now(UTC),
                        job_id=job.id,
                        chat_id=chat_id,
                        message=(
                            "нет медиа для скачивания — попробуйте only_new=false "
                            "или расширьте окно дат"
                        ),
                        level="warn",
                    ),
                )

            # Honest 100% — the only place allowed to emit it (per-window
            # heartbeats clamp to 99 %). Flips the UI off the last seed.
            if total_kept > 0:
                await manager.emit(
                    job,
                    JobUpdate(
                        kind=JobUpdateKind.PROGRESS,
                        ts=datetime.now(UTC),
                        job_id=job.id,
                        chat_id=chat_id,
                        percent=100.0,
                        current=int(total_kept),
                        total=int(total_kept),
                    ),
                )

            dedup_stats = DedupBatchStats()
            if settings.dedup and total_kept > 0:
                await manager.emit(
                    job,
                    JobUpdate(
                        kind=JobUpdateKind.STATUS,
                        ts=datetime.now(UTC),
                        job_id=job.id,
                        chat_id=chat_id,
                        status="deduping",
                    ),
                )
                await manager.emit(
                    job,
                    JobUpdate(
                        kind=JobUpdateKind.LOG,
                        ts=datetime.now(UTC),
                        job_id=job.id,
                        chat_id=chat_id,
                        message="дедупликация (sha256 + hardlinks)…",
                        level="info",
                    ),
                )
                dedup_stats = await self._dedup.process_directory(chat_out)
                await manager.emit(
                    job,
                    JobUpdate(
                        kind=JobUpdateKind.LOG,
                        ts=datetime.now(UTC),
                        job_id=job.id,
                        chat_id=chat_id,
                        message=(
                            f"дедуп: {dedup_stats.duplicates} дубликатов, "
                            f"{dedup_stats.bytes_saved // 1048576} MiB сэкономлено"
                        ),
                        level="info",
                    ),
                )

            # Advance the watermark to the swept tip (chunked) or the max id
            # seen (recent mode), and clear the in-progress cursor — the sweep
            # is complete.
            final_id = sweep_tip if sweep_tip is not None else overall_max_id
            await self._advance_state(
                chat_id,
                final_id,
                files=total_files,
                bytes_=total_bytes,
                clear_cursor=True,
            )

            job.files_deduped += dedup_stats.duplicates
            job.bytes_saved += dedup_stats.bytes_saved

            # Enrich history with from_id/me_id via Telethon (if a manager
            # is wired up + authorised) so the HTML can split bubbles into
            # in/out columns. Optional — failure is non-fatal.
            history_path = chat_dir / "messages.json"
            html_path = chat_dir / "messages.html"
            tg_mgr = self._tg_provider() if self._tg_provider else None
            if tg_mgr is not None and history_path.exists() and self._avatars_dir is not None:
                await manager.emit(
                    job,
                    JobUpdate(
                        kind=JobUpdateKind.LOG,
                        ts=datetime.now(UTC),
                        job_id=job.id,
                        chat_id=chat_id,
                        message="enrich: подтягиваю from_id/me_id через Telethon…",
                        level="info",
                    ),
                )
                try:
                    from src.jobs.history_enrich import enrich_history

                    enrich_stats = await enrich_history(
                        chat_id=chat_id,
                        messages_json=history_path,
                        avatars_dir=self._avatars_dir,
                        manager=tg_mgr,
                    )
                    log.info(
                        "history_enriched",
                        chat_id=chat_id,
                        enriched=enrich_stats.enriched,
                        total=enrich_stats.total,
                    )
                    await manager.emit(
                        job,
                        JobUpdate(
                            kind=JobUpdateKind.LOG,
                            ts=datetime.now(UTC),
                            job_id=job.id,
                            chat_id=chat_id,
                            message=(
                                f"enrich завершён: {enrich_stats.enriched} из "
                                f"{enrich_stats.total} сообщений обогащены"
                            ),
                            level="info",
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("history_enrich_failed", chat_id=chat_id, error=str(exc))
                    await manager.emit(
                        job,
                        JobUpdate(
                            kind=JobUpdateKind.LOG,
                            ts=datetime.now(UTC),
                            job_id=job.id,
                            chat_id=chat_id,
                            message=f"enrich пропущен: {type(exc).__name__}: {exc}",
                            level="warn",
                        ),
                    )

            # Render a browsable messages.html alongside messages.json so
            # the user has a readable transcript without needing extra tools.
            try:
                if history_path.exists():
                    _render_history_html(
                        history_path,
                        html_path,
                        title=title,
                        avatars_dir=self._avatars_dir,
                    )
                    log.info(
                        "history_html_rendered",
                        chat_id=chat_id,
                        bytes=html_path.stat().st_size,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("history_html_render_failed", chat_id=chat_id, error=str(exc))

    # -------- per-window export pipeline --------

    async def _probe_tip(
        self,
        *,
        chat_id: int,
        candidates: list[str | int],
        settings: JobSettings,
        tmp_dir: Path,
        job: Job,
        manager: JobManager,
    ) -> int | None:
        """Cheaply find the chat's newest message id via ``-T last -i 1``.

        Returns the tip id, or None for an empty chat. Raises (after the
        bounded retry loop) if even this one-message export can't complete —
        treated the same as a failed export.
        """
        probe_out = tmp_dir / f"chat_{chat_id}_tip.json"
        await self._chat_export_with_retry(
            candidates=candidates,
            settings=settings,
            output=probe_out,
            from_id=None,
            to_id=None,
            last_n=1,
            job=job,
            manager=manager,
            chat_id=chat_id,
            what="tip-probe",
        )
        try:
            data = json.loads(probe_out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        msgs = data.get("messages") or data.get("Messages") or []
        ids = [
            m.get("ID") or m.get("id") or m.get("Id") or m.get("message_id")
            for m in msgs
            if isinstance(m, dict)
        ]
        ids = [i for i in ids if isinstance(i, int)]
        return max(ids) if ids else None

    async def _chat_export_with_retry(
        self,
        *,
        candidates: list[str | int],
        settings: JobSettings,
        output: Path,
        from_id: int | None,
        to_id: int | None,
        last_n: int | None,
        job: Job,
        manager: JobManager,
        chat_id: int,
        what: str = "export",
    ) -> str | int:
        """Run ``tdl chat export`` for one range, recovering from four
        distinct failure modes, and return the chat-handle candidate that
        worked (so the caller can pin it for subsequent windows).

        Failure modes, each with its own bounded retry:
          * bolt-DB lock ("used by another process") — tdl's bolt is
            single-writer; a crashed child can hold the lock briefly.
          * FloodWait — Telegram-mandated countdown; sleep it, retry the
            same handle.
          * transient network timeout ("context deadline exceeded",
            "retry limit reached", i/o timeout, conn reset) — tdl drops
            mid-export under throttling; the same call usually succeeds on
            retry. Without this a single timeout failed the WHOLE job
            (user report: @rabbitmanson 881859567 only_new export).
          * empty peer ("got empty result" / PEER_ID_INVALID) — try the
            next chat-id format candidate.
        """
        _MAX_LOCK = 3
        _MAX_TRANSIENT = 4
        _MAX_FLOOD = 3
        last_exc: Exception | None = None
        cand_idx = 0
        lock_attempts = 0
        transient_attempts = 0
        flood_rounds = 0
        while cand_idx < len(candidates):
            cand = candidates[cand_idx]
            try:
                await self._tdl.chat_export(
                    chat=cand,
                    output=output,
                    from_id=from_id,
                    to_id=to_id,
                    last_n=last_n,
                    with_content=settings.with_content,
                    include_all=True,
                )
                return cand
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                msg = str(exc)
                low = msg.lower()
                is_locked = (
                    "used by another process" in low
                    or "database is locked" in low
                )
                is_empty_peer = (
                    "got empty result for chat" in low
                    or "peer_id_invalid" in low
                )
                is_transient = any(
                    s in low
                    for s in (
                        "context deadline exceeded",
                        "retry limit reached",
                        "i/o timeout",
                        "connection reset",
                        "connection refused",
                        "broken pipe",
                        "unexpected eof",
                        "no such host",
                        "timeout awaiting",
                    )
                )
                flood_wait_s = _flood_wait_seconds(exc)

                if is_locked and lock_attempts < _MAX_LOCK - 1:
                    lock_attempts += 1
                    log.warning(
                        "exporter_chat_export_lock_retry",
                        chat_id=chat_id,
                        what=what,
                        attempt=lock_attempts,
                        error=msg[:200],
                    )
                    await manager.emit(
                        job,
                        JobUpdate(
                            kind=JobUpdateKind.LOG,
                            ts=datetime.now(UTC),
                            job_id=job.id,
                            chat_id=chat_id,
                            message=f"tdl bolt-DB locked (попытка {lock_attempts}/{_MAX_LOCK}) — повтор через {2*lock_attempts}s",
                            level="warn",
                        ),
                    )
                    await asyncio.sleep(2 * lock_attempts)
                    continue
                if flood_wait_s is not None and flood_rounds < _MAX_FLOOD:
                    flood_rounds += 1
                    wait_s = min(max(int(flood_wait_s), 5), 900)
                    log.warning(
                        "exporter_chat_export_flood_wait",
                        chat_id=chat_id,
                        what=what,
                        round=flood_rounds,
                        wait_seconds=wait_s,
                    )
                    await manager.emit(
                        job,
                        JobUpdate(
                            kind=JobUpdateKind.LOG,
                            ts=datetime.now(UTC),
                            job_id=job.id,
                            chat_id=chat_id,
                            message=(
                                f"FloodWait {wait_s}s от Telegram на экспорте — жду и "
                                f"повторяю (попытка {flood_rounds}/{_MAX_FLOOD})…"
                            ),
                            level="warn",
                        ),
                    )
                    await asyncio.sleep(wait_s)
                    continue
                if is_transient and transient_attempts < _MAX_TRANSIENT - 1:
                    transient_attempts += 1
                    delay = min(120.0, 5.0 * (2 ** (transient_attempts - 1)))
                    log.warning(
                        "exporter_chat_export_transient_retry",
                        chat_id=chat_id,
                        what=what,
                        attempt=transient_attempts,
                        delay_seconds=delay,
                        error=msg[:200],
                    )
                    await manager.emit(
                        job,
                        JobUpdate(
                            kind=JobUpdateKind.LOG,
                            ts=datetime.now(UTC),
                            job_id=job.id,
                            chat_id=chat_id,
                            message=(
                                f"tdl экспорт прервался (таймаут/сеть) — "
                                f"повтор #{transient_attempts}/{_MAX_TRANSIENT} через {delay:.0f}s"
                            ),
                            level="warn",
                        ),
                    )
                    await asyncio.sleep(delay)
                    continue
                if is_empty_peer and cand_idx + 1 < len(candidates):
                    next_cand = candidates[cand_idx + 1]
                    log.warning(
                        "exporter_chat_id_fallback",
                        chat_id=chat_id,
                        tried=cand,
                        trying=next_cand,
                        error=msg[:200],
                    )
                    await manager.emit(
                        job,
                        JobUpdate(
                            kind=JobUpdateKind.LOG,
                            ts=datetime.now(UTC),
                            job_id=job.id,
                            chat_id=chat_id,
                            message=(
                                f"tdl не нашёл peer {cand!r} — пробую "
                                f"альтернативный формат {next_cand!r}"
                            ),
                            level="warn",
                        ),
                    )
                    cand_idx += 1
                    lock_attempts = 0
                    transient_attempts = 0
                    flood_rounds = 0
                    continue
                break

        # Exhausted all candidates / retries.
        log.exception(
            "exporter_chat_export_failed",
            chat_id=chat_id,
            what=what,
            error=str(last_exc),
            exc_info=last_exc,
        )
        await manager.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.LOG,
                ts=datetime.now(UTC),
                job_id=job.id,
                chat_id=chat_id,
                message=f"tdl chat export failed ({what}): {type(last_exc).__name__}: {last_exc}",
                level="error",
            ),
        )
        assert last_exc is not None
        raise last_exc

    async def _export_one_window(
        self,
        *,
        chat_id: int,
        candidates: list[str | int],
        settings: JobSettings,
        from_id: int | None,
        to_id: int | None,
        last_n: int | None,
        tmp_dir: Path,
        chat_dir: Path,
        chat_out: Path,
        title: str | None,
        job: Job,
        manager: JobManager,
        window_index: int,
        window_total: int,
        window_label: str,
    ) -> _WindowResult:
        """Export one id-range (or last-N) window, filter it, download its
        media into ``chat_out`` and merge its messages into messages.json.

        Returns a :class:`_WindowResult`. Raises only on an unrecoverable
        export/download failure (so the job is marked FAILED).
        """
        raw_json = tmp_dir / f"chat_{chat_id}_w{window_index}.json"

        if window_total > 1:
            rng = f"id {from_id}..{to_id}" if from_id is not None else "recent"
            await manager.emit(
                job,
                JobUpdate(
                    kind=JobUpdateKind.LOG,
                    ts=datetime.now(UTC),
                    job_id=job.id,
                    chat_id=chat_id,
                    message=f"окно {window_index}/{window_total} ({rng}) — экспорт…",
                    level="info",
                ),
            )

        resolved = await self._chat_export_with_retry(
            candidates=candidates,
            settings=settings,
            output=raw_json,
            from_id=from_id,
            to_id=to_id,
            last_n=last_n,
            job=job,
            manager=manager,
            chat_id=chat_id,
            what=f"window {window_index}/{window_total}",
        )

        raw_size = raw_json.stat().st_size if raw_json.exists() else 0
        try:
            _data_for_count = json.loads(raw_json.read_text(encoding="utf-8")) if raw_size else {}
        except json.JSONDecodeError:
            _data_for_count = {}
        raw_msg_count = len(
            _data_for_count.get("messages") or _data_for_count.get("Messages") or []
        )
        log.info(
            "exporter_chat_export_done",
            job_id=job.id,
            chat_id=chat_id,
            window=window_label,
            raw_json_bytes=raw_size,
            raw_messages=raw_msg_count,
        )

        filtered_manifest = tmp_dir / f"chat_{chat_id}_w{window_index}.manifest.json"
        count, max_id = _filter_manifest(
            raw_json,
            filtered_manifest,
            media_types=settings.media_types,
            max_size=settings.max_file_size_bytes,
            date_from=settings.date_from,
            date_to=settings.date_to,
        )
        log.info(
            "exporter_filter_done",
            job_id=job.id,
            chat_id=chat_id,
            window=window_label,
            kept=count,
            max_message_id=max_id,
        )

        # Persist/merge the message history for this window (text + media
        # refs), even when no media passed the filter — the transcript should
        # hold every message. The merge-by-id in persist_message_history makes
        # this idempotent across windows and re-runs.
        try:
            _persist_message_history(raw_json, chat_dir / "messages.json", title=title)
        except Exception as exc:  # noqa: BLE001
            log.warning("history_json_persist_failed", chat_id=chat_id, error=str(exc))

        if count == 0:
            return _WindowResult(
                kept=0, max_id=max_id, files=0, bytes=0, resolved_handle=resolved
            )

        await manager.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.STATUS,
                ts=datetime.now(UTC),
                job_id=job.id,
                chat_id=chat_id,
                status="downloading",
            ),
        )
        # Seed progress so the bar flips from "0/0" to "0/N" before the first
        # tdl progress line arrives.
        await manager.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.PROGRESS,
                ts=datetime.now(UTC),
                job_id=job.id,
                chat_id=chat_id,
                percent=0.0,
                current=0,
                total=int(count),
            ),
        )

        def _dl_progress_cb(event: object) -> None:
            from src.services.tdl_types import TdlProgress

            if not isinstance(event, TdlProgress):
                return
            asyncio.create_task(
                manager.emit(
                    job,
                    JobUpdate(
                        kind=JobUpdateKind.PROGRESS,
                        ts=datetime.now(UTC),
                        job_id=job.id,
                        chat_id=chat_id,
                        percent=float(event.percent),
                        current=int(event.current),
                        total=int(event.total),
                    ),
                )
            )

        # Fallback heartbeat — tdl stays silent for ``--skip-same`` hits
        # (already-downloaded hardlinks), so poll the on-disk file count.
        # ``chat_out`` ACCUMULATES across windows, so baseline off the count
        # present when this window starts and report only this window's new
        # files. Cap at count-1: the honest 100 % comes once, after the whole
        # sweep, from the orchestrator.
        try:
            baseline = sum(1 for _ in chat_out.iterdir() if _.is_file())
        except OSError:
            baseline = 0
        heartbeat_stop = asyncio.Event()
        heartbeat_cap = max(0, int(count) - 1)

        async def _file_count_heartbeat() -> None:
            interval_s = 2.0
            last_seen = -1
            while not heartbeat_stop.is_set():
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(), timeout=interval_s)
                    return
                except TimeoutError:
                    pass
                try:
                    n = sum(1 for _ in chat_out.iterdir() if _.is_file()) - baseline
                except OSError:
                    continue
                n = max(0, min(n, heartbeat_cap))
                if n == last_seen:
                    continue
                last_seen = n
                pct = min((n / count * 100.0) if count else 0.0, 99.0)
                await manager.emit(
                    job,
                    JobUpdate(
                        kind=JobUpdateKind.PROGRESS,
                        ts=datetime.now(UTC),
                        job_id=job.id,
                        chat_id=chat_id,
                        percent=pct,
                        current=n,
                        total=int(count),
                    ),
                )

        heartbeat_task = asyncio.create_task(
            _file_count_heartbeat(), name=f"dl-heartbeat-{chat_id}-w{window_index}"
        )

        # Resilient download: ``tdl dl`` exits 1 under transient throttling
        # ("context deadline exceeded" / "retry limit reached"). ``--skip-same``
        # makes every retry cheap (hardlinks, not re-fetch). with_retry backs
        # off; a real FLOOD_WAIT raises FloodWaitNotRetried so we sleep the
        # countdown rather than hammering.
        dl_invocations = 0

        async def _run_dl_once():  # noqa: ANN202 — DlResult, duck-typed
            nonlocal dl_invocations
            dl_invocations += 1
            if dl_invocations > 1:
                await manager.emit(
                    job,
                    JobUpdate(
                        kind=JobUpdateKind.LOG,
                        ts=datetime.now(UTC),
                        job_id=job.id,
                        chat_id=chat_id,
                        message=(
                            f"скачивание: повторная попытка #{dl_invocations} "
                            f"после сбоя tdl (троттлинг/сеть)…"
                        ),
                        level="warn",
                    ),
                )
            return await self._tdl.dl(
                manifest=filtered_manifest,
                output_dir=chat_out,
                threads=settings.threads_per_file,
                limit=settings.parallel_tasks,
                skip_same=True,
                on_progress=_dl_progress_cb,
            )

        try:
            flood_rounds = 0
            while True:
                try:
                    dl_result = await with_retry(
                        _run_dl_once,
                        max_attempts=4,
                        base_delay=5.0,
                        max_delay=120.0,
                    )
                    break
                except FloodWaitNotRetried as fw:
                    flood_rounds += 1
                    wait_s = min(max(int(fw.wait_seconds), 5), 900)
                    if flood_rounds > 3:
                        raise
                    await manager.emit(
                        job,
                        JobUpdate(
                            kind=JobUpdateKind.LOG,
                            ts=datetime.now(UTC),
                            job_id=job.id,
                            chat_id=chat_id,
                            message=(
                                f"FloodWait {wait_s}s от Telegram — жду и "
                                f"продолжаю скачивание (попытка {flood_rounds}/3)…"
                            ),
                            level="warn",
                        ),
                    )
                    await asyncio.sleep(wait_s)
        finally:
            heartbeat_stop.set()
            try:
                await heartbeat_task
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

        await manager.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.LOG,
                ts=datetime.now(UTC),
                job_id=job.id,
                chat_id=chat_id,
                message=(
                    f"скачивание окна {window_index}/{window_total} завершено: "
                    f"{dl_result.files_downloaded or count} файлов"
                    f"{'' if not dl_result.bytes_total else f', {dl_result.bytes_total // 1048576} MiB'}"
                ),
                level="info",
            ),
        )

        return _WindowResult(
            kept=count,
            max_id=max_id,
            files=dl_result.files_downloaded,
            bytes=dl_result.bytes_total,
            resolved_handle=resolved,
        )

    # -------- DB helpers --------

    async def _load_state(self, chat_id: int) -> ChatState | None:
        async with self._sf() as s:
            row = (
                await s.execute(select(ChatState).where(ChatState.chat_id == chat_id))
            ).scalar_one_or_none()
        return row

    async def _chat_title_for(self, chat_id: int) -> str | None:
        """Look up the readable title (or @username) for the chat — used
        for the export directory name. None if the chat row is missing."""
        async with self._sf() as s:
            row = (
                await s.execute(select(Chat).where(Chat.id == chat_id))
            ).scalar_one_or_none()
        if row is None:
            return None
        return row.title or (f"@{row.username}" if row.username else None)

    async def _chat_handle_for(self, chat_id: int) -> str | int:
        """Pick the most reliable handle to pass to tdl `-c`.

        Order of preference:
        1. `@username` if the chat has one (works without a primed peer cache).
        2. tdl-format numeric id — Telethon emits supergroups/channels with a
           leading `-100` prefix (`-1009876543210`), tdl wants the raw
           positive `5551237890`. We convert.
        """
        async with self._sf() as s:
            row = (
                await s.execute(select(Chat).where(Chat.id == chat_id))
            ).scalar_one_or_none()
        if row is not None and row.username:
            return f"@{row.username}"
        return _to_tdl_id(chat_id)

    async def _advance_chunk_cursor(self, chat_id: int, window_hi: int) -> None:
        """Persist the durable resume point after a window completes.

        Only moves forward. Creates the Chat/ChatState rows if missing so the
        cursor survives a crash before the first watermark advance.
        """
        async with self._sf() as s, transaction(s):
            row = (
                await s.execute(select(ChatState).where(ChatState.chat_id == chat_id))
            ).scalar_one_or_none()
            if row is None:
                chat = (
                    await s.execute(select(Chat).where(Chat.id == chat_id))
                ).scalar_one_or_none()
                if chat is None:
                    chat = Chat(id=chat_id, title=str(chat_id))
                    s.add(chat)
                    await s.flush()
                row = ChatState(chat_id=chat_id)
                s.add(row)
            if (
                row.export_cursor_message_id is None
                or window_hi > row.export_cursor_message_id
            ):
                row.export_cursor_message_id = window_hi

    async def _advance_state(
        self,
        chat_id: int,
        max_id: int | None,
        *,
        files: int,
        bytes_: int,
        clear_cursor: bool = False,
    ) -> None:
        async with self._sf() as s, transaction(s):
            row = (
                await s.execute(select(ChatState).where(ChatState.chat_id == chat_id))
            ).scalar_one_or_none()
            if row is None:
                # Make sure a Chat row exists — state is FK-constrained.
                chat = (
                    await s.execute(select(Chat).where(Chat.id == chat_id))
                ).scalar_one_or_none()
                if chat is None:
                    chat = Chat(id=chat_id, title=str(chat_id))
                    s.add(chat)
                    await s.flush()
                row = ChatState(chat_id=chat_id)
                s.add(row)
            if max_id is not None and (
                row.last_exported_message_id is None or max_id > row.last_exported_message_id
            ):
                row.last_exported_message_id = max_id
            if clear_cursor:
                # The sweep is complete (or there was nothing to sweep): drop
                # the in-progress resume cursor.
                row.export_cursor_message_id = None
            row.last_export_at = datetime.now(UTC)
            row.total_size_bytes = (row.total_size_bytes or 0) + bytes_
            row.total_files = (row.total_files or 0) + files


# --------- module-level helpers ---------


def _chunk_start(state: ChatState | None, settings: JobSettings) -> int:
    """First message id to export for a chunked sweep.

    Precedence:
    1. A live ``export_cursor_message_id`` — resume an interrupted sweep from
       just past the last fully-completed window.
    2. ``only_new`` watermark — incremental sync from just past the last
       committed export.
    3. 0 — full backfill.
    """
    if state is not None and state.export_cursor_message_id is not None:
        return state.export_cursor_message_id + 1
    if settings.only_new and state is not None and state.last_exported_message_id:
        return state.last_exported_message_id + 1
    return 0


def _flood_wait_seconds(exc: Exception) -> int | None:
    """Return the FLOOD_WAIT countdown (seconds) carried by a tdl error.

    ``TdlSubprocessError`` carries a list of structured ``TdlError`` events;
    a Telegram flood wait surfaces as one with ``kind == "flood_wait"`` and a
    ``wait_seconds``. Returns None when no flood-wait error is present (so the
    caller can fall through to its transient/lock/empty-peer handling).
    """
    for e in getattr(exc, "errors", None) or []:
        if getattr(e, "kind", None) == "flood_wait":
            return int(getattr(e, "wait_seconds", 0) or 0)
    return None


def _to_tdl_id(telethon_id: int) -> int:
    """Convert a Telethon-style chat id to the positive raw id tdl uses.

    Telethon emits:
      - users / bots:    positive int               (e.g. 7712345678)
      - basic groups:   negative int  > -10**11    (e.g. -212003)
      - supergroups/channels: negative int < -10**11 (e.g. -1009876543210,
                                                      i.e. -100 + raw)

    `tdl chat ls -o json` lists those same supergroups/channels with the
    raw positive id (9876543210 in the example) and `chat export -c <id>`
    expects that form. So we strip the leading -100 marker by adding 10**12
    and taking absolute value.
    """
    if telethon_id < -(10 ** 12):
        return abs(telethon_id) - (10 ** 12)
    return telethon_id


def _tdl_chat_handle_candidates(primary: str | int) -> list[str | int]:
    """Build the ordered list of chat handles we'll feed to ``tdl
    chat export -c <handle>`` if the first one fails with "got empty
    result".

    Background: tdl's bolt-DB peer cache occasionally indexes a chat
    under a different marked-id format than Telethon's
    ``dialog.id``. The most common case is a basic group whose
    Telegram-server id exceeds int32: from Telethon's side the dialog
    still looks like a basic Chat (id = -<raw>, ChatType.GROUP), but
    tdl's cache has it under the supergroup format
    (id = -100<raw> or raw positive). We can't always tell ahead of
    time which form tdl knows about, so we try several in order and
    let tdl reject the bad ones with a fast "empty result".

    @username always wins when present — tdl resolves usernames
    server-side without touching its peer cache.
    """
    if isinstance(primary, str):
        # @username — no fallback; if Telegram doesn't recognise the
        # username, retrying with junk numeric ids won't help.
        return [primary]

    seen: set[int] = {primary}
    out: list[str | int] = [primary]
    if primary < 0:
        abs_id = abs(primary)
        # Supergroup-style marked id: -100<abs_id>. Useful when the
        # chat was a basic group that got migrated to a supergroup
        # but our DB still has the basic-Chat id.
        for cand in (-(10 ** 12 + abs_id), abs_id):
            if cand not in seen:
                out.append(cand)
                seen.add(cand)
    return out


def _chat_slug(chat_id: int, title: str | None = None) -> str:
    """Safe folder name for a chat.

    Format: ``chat_<slug>_<id>`` — slug is the chat title sanitised to
    file-system-safe characters; the numeric id is always appended so
    collisions between two chats with the same title can never lose data.
    Falls back to plain ``chat_<id>`` when no title is available.
    """
    if not title:
        return f"chat_{chat_id}"

    try:
        from slugify import slugify  # python-slugify
    except ImportError:  # pragma: no cover — defensive
        return f"chat_{chat_id}"

    # `lowercase=False` keeps Cyrillic/Latin case readable; `separator='_'`
    # plays nicely with shell tooling. `max_length` keeps Windows MAX_PATH
    # friendly (260 chars total = directory + filename).
    slug = slugify(title, max_length=60, lowercase=False, separator="_")
    if not slug:
        return f"chat_{chat_id}"
    return f"chat_{slug}_{chat_id}"


def _media_extension(media_obj) -> str | None:  # noqa: ANN001
    """Get the lowercase extension from a tdl media field.

    `media_obj` can be:
      - a dict (with-content schema): {Name, Size, DC, ...}
      - a plain string (minimal schema): "IMG_0791.mp4"
      - None / empty
    """
    if not media_obj:
        return None
    if isinstance(media_obj, str):
        name = media_obj
    elif isinstance(media_obj, dict):
        name = (
            media_obj.get("Name")
            or media_obj.get("name")
            or media_obj.get("file_name")
        )
    else:
        return None
    if not isinstance(name, str) or "." not in name:
        return None
    return name.rsplit(".", 1)[-1].lower()


def _media_matches(media_obj, allowed_exts: Iterable[str]) -> bool:  # noqa: ANN001
    """Return True if the file extension on the media payload is allowed.
    `allowed_exts=()` means 'any media'. Tolerates string OR dict media."""
    if not media_obj:
        return False
    allowed = set(allowed_exts)
    if not allowed:
        return True
    ext = _media_extension(media_obj)
    if ext is None:
        # No filename / no extension → keep, tdl will figure it out.
        return True
    return ext in allowed


def _filter_manifest(
    src_json: Path,
    dst_json: Path,
    *,
    media_types: list[MediaType],
    max_size: int | None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[int, int | None]:
    """Filter a tdl chat-export JSON into a manifest `tdl dl -f` accepts.

    Important: tdl dl reads top-level fields (`id`, `type`, peer info) to
    resolve which chat to download from. Earlier we wrote a JSON containing
    only `{"messages": [...]}` — tdl then complained 'can't get chat type
    or chat id'. We now preserve all top-level fields and replace only
    `messages` with the filtered subset.

    Returns `(count, max_msg_id)` where count is the number of media items kept.
    Missing / malformed JSON returns (0, None).
    """
    if not src_json.exists() or src_json.stat().st_size == 0:
        dst_json.write_text(json.dumps({"messages": []}), encoding="utf-8")
        return 0, None

    try:
        data = json.loads(src_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("tdl_export_json_unparseable", path=str(src_json))
        dst_json.write_text(json.dumps({"messages": []}), encoding="utf-8")
        return 0, None

    allowed_exts: list[str] = []
    for mt in media_types:
        allowed_exts.extend(_TDL_MEDIA_EXTS.get(mt, ()))

    # Parse the optional date window. Accept "YYYY-MM-DD" and full ISO 8601.
    from datetime import datetime as _dt

    def _parse_iso(s: str | None) -> _dt | None:
        if not s:
            return None
        try:
            return _dt.fromisoformat(s.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    df = _parse_iso(date_from)
    dt_ = _parse_iso(date_to)
    # Normalise to naive (UTC) for comparison with tdl's `date` (epoch seconds).
    df_ts = df.timestamp() if df else None
    dt_ts = dt_.timestamp() if dt_ else None

    messages_key = "messages" if "messages" in data else ("Messages" if "Messages" in data else "messages")
    messages = data.get("messages") or data.get("Messages") or []
    kept: list[dict] = []
    max_id: int | None = None
    for msg in messages:
        # tdl emits TWO different schemas depending on flags:
        #   * with `--all`:     {ID, Date, Message, Media: {Name, Size, DC}}
        #   * minimal (default): {id, type, file: {Name, Size, DC}}
        # Be tolerant of both.
        mid = (
            msg.get("ID") or msg.get("id") or msg.get("Id") or msg.get("message_id")
        )
        if isinstance(mid, int):
            max_id = mid if max_id is None else max(max_id, mid)
        media = (
            msg.get("Media")
            or msg.get("media")
            or msg.get("file")
            or msg.get("File")
        )
        if media is None:
            continue
        if not _media_matches(media, allowed_exts):
            continue
        # Size only available in the with-content schema (dict media).
        size = None
        if isinstance(media, dict):
            size = (
                media.get("Size")
                or media.get("size")
                or media.get("file_size")
            )
        if max_size is not None and isinstance(size, int) and size > max_size:
            continue
        # Date-window filter — `Date` in tdl export is epoch seconds.
        if df_ts is not None or dt_ts is not None:
            mdate = msg.get("Date") or msg.get("date")
            if isinstance(mdate, (int, float)):
                if df_ts is not None and mdate < df_ts:
                    continue
                if dt_ts is not None and mdate > dt_ts:
                    continue
        kept.append(msg)

    # Preserve top-level fields (id, type, peer, etc.) so tdl dl can resolve
    # the source chat. Only the messages array is replaced.
    data[messages_key] = kept
    dst_json.write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(kept), max_id
