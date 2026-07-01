"""Per-chat auto-update background scheduler.

A long-running asyncio task polls every watched chat and enqueues an
incremental (``only_new``) export job whenever the chat has fallen behind
its last export. It is the unattended counterpart to the manual "Синхр."
button wired in ``routes_chats`` — same staleness rule, same per-chat dedup,
same ``JobSettings(only_new=True, dedup=True)`` payload.

Design
------
* ``_tick()`` runs exactly one poll pass and returns how many jobs it
  submitted. Tests drive this directly so they never have to race the
  infinite loop.
* ``run_periodic(...)`` is the lifespan-owned loop: sleep-first, then call
  ``_tick``, with every iteration body wrapped so a single bad pass can
  never kill the ticker (mirrors ``ProxyPool.run_periodic_retest``).
* Cancellation is re-raised out of the loop so the composition root can
  ``await`` the task to completion during teardown — critical on Windows,
  where a task still holding an aiosqlite session past ``engine.dispose()``
  deadlocks the event loop (see ``detach_runtime``).

Staleness rule (kept byte-for-byte identical to ``routes_chats``):
a chat is "behind" when it has been exported before
(``last_exported_message_id`` and ``last_export_at`` both set) AND Telegram
has a fresher message than that export (``last_message_date >
last_export_at``). SQLite hands back ``DateTime(timezone=True)`` columns as
naive values, so every timestamp is coerced to UTC before comparison.

tdl-login gating
----------------
Auto-export goes through tdl, which has its OWN session independent of
Telethon. Telethon being authorised says nothing about whether tdl can
download, so an optional ``tdl_login_check`` lets the caller veto the whole
pass when tdl is not logged in. It is optional precisely so unit tests (and
the first-cut composition wiring) don't have to stand up a real tdl session.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from src.db.models import Chat, ChatState
from src.db.session import transaction
from src.jobs.models import JobSettings
from src.logging_setup import get_logger

if TYPE_CHECKING:
    from src.db.session import SessionFactory
    from src.jobs.job_manager import JobManager

log = get_logger(__name__)

# Default cadence of the lifespan loop, in seconds. Each tick re-evaluates
# every watched chat; the *per-chat* interval (``watch_interval_seconds`` or
# ``default_interval_seconds``) is what actually rate-limits submissions, so
# the loop can tick relatively often without spamming jobs.
DEFAULT_TICK_SECONDS = 120

# Statuses a job occupies while it is still "in flight". Mirrors
# ``routes_chats._ACTIVE_SYNC_STATUSES`` so the scheduler's dedup scan and
# the chat-row "⟳ Идёт" badge agree on what counts as already-syncing.
_ACTIVE_SYNC_STATUSES = frozenset(
    {"pending", "running", "exporting", "downloading", "deduping"}
)


def _aware(dt: datetime | None) -> datetime | None:
    """Coerce a naive ``datetime`` to UTC; pass through aware / ``None``.

    SQLite stores ``DateTime(timezone=True)`` columns without tz info, so a
    round-tripped timestamp comes back naive and is not directly comparable
    to ``datetime.now(UTC)``. Replicated from ``routes_chats._aware``.
    """
    return dt if (dt is None or dt.tzinfo is not None) else dt.replace(tzinfo=UTC)


class AutoUpdateScheduler:
    """Polls watched chats and enqueues incremental sync jobs."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        job_manager: JobManager,
        default_interval_seconds: int = 3600,
        tdl_login_check: Callable[[], Awaitable[bool]]
        | Callable[[], bool]
        | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._job_manager = job_manager
        self._default_interval_seconds = default_interval_seconds
        self._tdl_login_check = tdl_login_check
        self._now_fn: Callable[[], datetime] = now_fn or (lambda: datetime.now(UTC))

    # -------- dedup helper --------

    def _active_chat_ids(self) -> set[int]:
        """Chat ids that already have an in-flight job.

        Mirrors the ``routes_chats`` defensive ``getattr`` access so a
        partially-initialised / restored Job can never raise here.
        """
        out: set[int] = set()
        for job in self._job_manager.list_jobs():
            status = getattr(getattr(job, "status", None), "value", None)
            if status not in _ACTIVE_SYNC_STATUSES:
                continue
            for cid in getattr(getattr(job, "settings", None), "chat_ids", None) or ():
                try:
                    out.add(int(cid))
                except (TypeError, ValueError):
                    continue
        return out

    # -------- one poll pass --------

    async def _tick(self) -> int:
        """Run one poll pass. Returns the number of jobs submitted.

        For every watched chat that is behind and not already syncing (and,
        when configured, only if tdl is logged in), submit an incremental
        export job and stamp ``watch_last_checked_at``.
        """
        now = self._now_fn()

        # The tdl-login gate is a whole-pass veto: cheaper to evaluate it
        # once than per chat, and an un-logged-in tdl means *nothing* can be
        # exported this pass.
        if self._tdl_login_check is not None and not await self._call_login_check():
            log.info("auto_update_skip_tdl_not_logged_in")
            return 0

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Chat, ChatState).join(
                        ChatState, ChatState.chat_id == Chat.id
                    )
                )
            ).all()

            active_ids = self._active_chat_ids()
            submitted = 0
            stamped_ids: list[int] = []

            for chat, state in rows:
                if not state.watch_enabled:
                    continue
                if not self._is_stale(chat, state):
                    continue

                interval = state.watch_interval_seconds or self._default_interval_seconds
                last_checked = _aware(state.watch_last_checked_at)
                if (
                    last_checked is not None
                    and (now - last_checked).total_seconds() < interval
                ):
                    continue

                if chat.id in active_ids:
                    log.info("auto_update_skip_already_syncing", chat_id=chat.id)
                    continue

                await self._job_manager.submit(
                    JobSettings(chat_ids=[chat.id], only_new=True, dedup=True)
                )
                submitted += 1
                stamped_ids.append(chat.id)
                # Reserve the id within this pass so two watched chats sharing
                # nothing don't collide and so a fast resubmit is impossible.
                active_ids.add(chat.id)
                log.info(
                    "auto_update_submitted",
                    chat_id=chat.id,
                    interval_seconds=interval,
                )

            if stamped_ids:
                async with transaction(session):
                    fresh = (
                        await session.execute(
                            select(ChatState).where(
                                ChatState.chat_id.in_(stamped_ids)
                            )
                        )
                    ).scalars()
                    for st in fresh:
                        st.watch_last_checked_at = now

        return submitted

    async def _call_login_check(self) -> bool:
        """Evaluate ``tdl_login_check``, awaiting it when it's a coroutine."""
        assert self._tdl_login_check is not None
        result = self._tdl_login_check()
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)

    def _is_stale(self, chat: Chat, state: ChatState) -> bool:
        """Has Telegram moved past this chat's last completed export?

        Identical predicate to ``routes_chats``: needs a prior export
        (watermark + timestamp) and a fresher Telegram message than that
        export's timestamp.
        """
        last_export = _aware(state.last_export_at)
        last_msg = _aware(chat.last_message_date)
        return bool(
            state.last_exported_message_id is not None
            and last_export is not None
            and last_msg is not None
            and last_msg > last_export
        )

    # -------- lifespan loop --------

    async def run_periodic(self, *, tick_seconds: int) -> None:
        """Long-running ticker: ``_tick`` every ``tick_seconds``.

        Sleep-first so a fresh boot doesn't immediately hammer tdl. Each
        iteration body is guarded so one failing pass never kills the loop;
        ``CancelledError`` is re-raised so the lifespan can await teardown.
        """
        try:
            while True:
                await asyncio.sleep(tick_seconds)
                try:
                    submitted = await self._tick()
                    if submitted:
                        log.info("auto_update_tick_done", submitted=submitted)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — never let the loop die
                    log.warning("auto_update_tick_failed", error=str(exc))
        except asyncio.CancelledError:
            log.info("auto_update_stopped")
            raise
