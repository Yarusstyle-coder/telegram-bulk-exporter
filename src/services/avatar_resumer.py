"""Resume unfinished avatar downloads after server restart.

Why
---
``_download_avatars_bg`` in :mod:`src.api.routes_chats` is fire-and-forget. If
the server dies mid-flight, three undesirable shapes can be left behind in
``settings.avatars_dir``:

1. **Missing files** — chat exists in DB but no file was ever attempted.
2. **Stale 0-byte placeholders** — written when Telegram had no profile photo
   *at the time*. The user may have set one since, so we want to give them
   another shot eventually.
3. **Corrupt JPEGs** — partial writes from an interrupted download. The file
   exists but its header is wrong, so the browser can't render it.

This module does a one-shot scan at app startup, reclassifies every file, and
re-enqueues anything that should be retried. It's wired into the FastAPI
lifespan by the composition root — kept here so the orchestration code stays
thin.

Public API
----------
- :class:`ResumeStats` — dataclass summarising what changed.
- :class:`AvatarResumer` — constructor takes ``session_factory``, ``avatars_dir``,
  and an optional ``placeholder_max_age_seconds`` (default 1 week).
- :meth:`AvatarResumer.scan_and_resume` — async entry point; returns a
  :class:`ResumeStats`.

Tests: see ``tests/unit/test_avatar_resumer.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any

from sqlalchemy import select

from src.db.models import Chat
from src.logging_setup import get_logger

log = get_logger(__name__)


# JPEG SOI + APP0/APP1 marker prefix. Real JPEGs always start with FF D8 FF.
_JPEG_MAGIC = b"\xff\xd8\xff"
# Cheap "is this a real JPEG?" threshold. Anything smaller can't possibly hold
# a valid header + minimal payload. Avoids false-positives on truncated writes.
_MIN_VALID_JPEG_BYTES = 64
# Cap parallel downloads. Avatars are tiny (~5 KiB each), so 4 is plenty.
_DOWNLOAD_CONCURRENCY = 4
# 1 week — placeholder is "fresh enough", don't bother Telegram again yet.
_DEFAULT_PLACEHOLDER_MAX_AGE_SECONDS = 7 * 86400


@dataclass(frozen=True)
class ResumeStats:
    """Outcome of one :meth:`AvatarResumer.scan_and_resume` pass.

    Attributes
    ----------
    total_chats:
        How many chat rows we walked.
    valid:
        Files that already had a healthy JPEG; left alone.
    redownloaded:
        Files we (re-)pulled from Telegram successfully — including ones that
        came back as ``None`` and got a fresh 0-byte placeholder.
    missing_after:
        Chats we attempted to download but couldn't get to a final state
        (download raised, or the entity vanished). Will be retried next run.
    placeholder_kept:
        Fresh 0-byte placeholders we deliberately left untouched.
    """

    total_chats: int
    valid: int
    redownloaded: int
    missing_after: int
    placeholder_kept: int


class AvatarResumer:
    """Reconcile the avatars directory against the chats table on startup.

    Construct once during app composition; call :meth:`scan_and_resume` after
    the Telegram manager is attached to ``app.state``. The class is stateless
    between calls — calling it twice is fine and idempotent on healthy state.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        avatars_dir: Path,
        placeholder_max_age_seconds: int = _DEFAULT_PLACEHOLDER_MAX_AGE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._avatars_dir = avatars_dir
        self._placeholder_max_age_seconds = placeholder_max_age_seconds

    async def scan_and_resume(self, telegram_manager: Any) -> ResumeStats:
        """Walk all chats, classify each avatar, and resume what's broken.

        Parameters
        ----------
        telegram_manager:
            Anything with ``async download_avatar(chat_id, dest) -> Path | None``.
            We deliberately don't import the concrete class so tests can pass an
            ``AsyncMock``.

        Returns
        -------
        ResumeStats:
            Counts of what happened. Useful for the lifespan log line.
        """
        self._avatars_dir.mkdir(parents=True, exist_ok=True)

        chat_ids = await self._load_chat_ids()

        valid = 0
        placeholder_kept = 0
        to_download: list[int] = []

        for chat_id in chat_ids:
            decision = self._classify(chat_id)
            if decision == "valid":
                valid += 1
            elif decision == "fresh_placeholder":
                placeholder_kept += 1
            else:
                # missing | stale_placeholder | corrupt — all need a retry.
                to_download.append(chat_id)

        redownloaded, missing_after = await self._download_many(
            telegram_manager, to_download
        )

        stats = ResumeStats(
            total_chats=len(chat_ids),
            valid=valid,
            redownloaded=redownloaded,
            missing_after=missing_after,
            placeholder_kept=placeholder_kept,
        )
        log.info(
            "avatar_resume_done",
            total_chats=stats.total_chats,
            valid=stats.valid,
            redownloaded=stats.redownloaded,
            missing_after=stats.missing_after,
            placeholder_kept=stats.placeholder_kept,
        )
        return stats

    # ----- internals -----

    async def _load_chat_ids(self) -> list[int]:
        async with self._session_factory() as session:
            rows = (await session.execute(select(Chat))).scalars().all()
        return [c.id for c in rows]

    def _classify(self, chat_id: int) -> str:
        """Return one of: ``valid``, ``fresh_placeholder``, ``stale_placeholder``,
        ``corrupt``, ``missing``.

        The path is computed from ``self._avatars_dir`` and ``chat_id``. Side
        effects (renaming a corrupt file to ``.bad``, deleting a stale
        placeholder) happen here so :meth:`scan_and_resume` stays a flat
        dispatcher.
        """
        dest = self._avatars_dir / f"{chat_id}.jpg"
        if not dest.exists():
            return "missing"

        try:
            size = dest.stat().st_size
        except OSError as exc:
            log.warning("avatar_stat_failed", chat_id=chat_id, error=str(exc))
            return "missing"

        if size == 0:
            age = self._file_age_seconds(dest)
            if age is not None and age > self._placeholder_max_age_seconds:
                self._safe_unlink(dest, chat_id, reason="stale_placeholder")
                return "stale_placeholder"
            return "fresh_placeholder"

        if not self._looks_like_jpeg(dest, size):
            # Quarantine the bad bytes — the user might want to inspect them.
            self._quarantine_corrupt(dest, chat_id)
            return "corrupt"

        return "valid"

    @staticmethod
    def _file_age_seconds(path: Path) -> float | None:
        try:
            return time() - path.stat().st_mtime
        except OSError:
            return None

    @staticmethod
    def _looks_like_jpeg(path: Path, size: int) -> bool:
        """Cheap JPEG-shape check. Doesn't decode, just validates header."""
        if size < _MIN_VALID_JPEG_BYTES:
            return False
        try:
            with path.open("rb") as fh:
                head = fh.read(len(_JPEG_MAGIC))
        except OSError:
            return False
        return head == _JPEG_MAGIC

    @staticmethod
    def _safe_unlink(path: Path, chat_id: int, *, reason: str) -> None:
        try:
            path.unlink()
        except OSError as exc:
            log.warning(
                "avatar_unlink_failed",
                chat_id=chat_id,
                reason=reason,
                error=str(exc),
            )

    @staticmethod
    def _quarantine_corrupt(path: Path, chat_id: int) -> None:
        """Rename a bad-header file to ``{chat_id}.jpg.bad`` instead of deleting.

        Keeps user data around in case it turns out to be a legitimate file with
        an unusual header — easier to debug than silently-missing bytes.
        """
        bad = path.with_suffix(path.suffix + ".bad")
        try:
            if bad.exists():
                bad.unlink()
            path.rename(bad)
        except OSError as exc:
            log.warning(
                "avatar_quarantine_failed",
                chat_id=chat_id,
                error=str(exc),
            )
            # Best effort: if rename failed, try removing the original so the
            # download attempt can write a fresh file.
            AvatarResumer._safe_unlink(path, chat_id, reason="quarantine_fallback")

    async def _download_many(
        self, telegram_manager: Any, chat_ids: list[int]
    ) -> tuple[int, int]:
        """Download avatars for every id in ``chat_ids``.

        Returns ``(redownloaded, missing_after)``.

        - ``redownloaded`` counts successful pulls *and* fresh 0-byte
          placeholders (Telegram returned ``None`` — not an error, just no
          photo).
        - ``missing_after`` counts attempts that raised. Those will be retried
          on the next startup.
        """
        if not chat_ids:
            return (0, 0)

        sem = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)

        async def one(chat_id: int) -> bool:
            async with sem:
                return await self._download_one(telegram_manager, chat_id)

        results = await asyncio.gather(
            *(one(cid) for cid in chat_ids), return_exceptions=False
        )
        redownloaded = sum(1 for r in results if r)
        missing_after = sum(1 for r in results if not r)
        return redownloaded, missing_after

    async def _download_one(self, telegram_manager: Any, chat_id: int) -> bool:
        """Attempt one download. Errors are caught and logged; never raises.

        Returns True if we reached a final state (file written or placeholder
        touched), False if the download itself failed.
        """
        dest = self._avatars_dir / f"{chat_id}.jpg"
        try:
            path = await telegram_manager.download_avatar(chat_id, dest)
        except Exception as exc:  # noqa: BLE001 — one bad chat must not poison the loop.
            log.warning(
                "avatar_resume_download_failed",
                chat_id=chat_id,
                error=str(exc),
            )
            return False

        if path is None:
            # No profile photo on TG. Drop a 0-byte placeholder so subsequent
            # scans treat it as "fresh" until the placeholder ages out.
            try:
                dest.write_bytes(b"")
            except OSError as exc:
                log.warning(
                    "avatar_placeholder_write_failed",
                    chat_id=chat_id,
                    error=str(exc),
                )
                return False
        return True
