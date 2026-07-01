"""SHA-256-based media deduplicator with hardlink-first strategy.

How it works
------------
The canonical store is `settings.media_pool_dir / <hash[0:2]>/<hash>.bin`.
When a new file arrives:

1. Hash it (streaming, 64 KiB chunks).
2. If hash is in the `media_files` table → `os.link()` the canonical file over
   the new path (removing the new file first). The incoming file's bytes are
   thrown away; the exported directory still contains a file at the expected
   path, but it's a hardlink to the pool. Saved bytes equal `size`.
3. If not in the table → move the file into the pool as canonical, then
   `os.link()` back to the expected path. Insert a new row.

Why hardlinks
-------------
- Transparent to every tool (players, Explorer, scripts).
- `st_size` counts once on disk but as N times across `du`-style summaries —
  so the user sees "no extra space" which is the promise.
- Deletion of any single link keeps the others.

Caveats
-------
- On Windows, hardlinks only work within a single NTFS volume. When the source
  and pool are on different volumes we fall back to a *copy* and log a
  warning — the caller's `bytes_saved` stat will be 0 for that file.
- If `os.link` raises for any other reason (permission, dst exists), we log
  and keep the original file untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from src.db.models import MediaFile
from src.db.session import SessionFactory
from src.logging_setup import get_logger

log = get_logger(__name__)

CHUNK = 64 * 1024


@dataclass(slots=True)
class DedupOutcome:
    sha256: str
    size: int
    was_duplicate: bool
    bytes_saved: int
    canonical_path: Path
    # `final_path` equals `target_path` the caller gave — after dedup it may
    # be a hardlink into the pool, but the path string is unchanged.
    final_path: Path


@dataclass(slots=True)
class DedupBatchStats:
    total_files: int = 0
    duplicates: int = 0
    bytes_saved: int = 0
    copy_fallbacks: int = 0


def hash_file(path: Path, *, chunk: int = CHUNK) -> str:
    """Return hex SHA-256 of the file contents."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _pool_path(pool_dir: Path, sha: str) -> Path:
    return pool_dir / sha[:2] / f"{sha}.bin"


def _same_volume(a: Path, b: Path) -> bool:
    """On Windows, compare drive letters; elsewhere, stat().st_dev."""
    try:
        if os.name == "nt":
            return str(a.resolve().drive).lower() == str(b.resolve().drive).lower()
        return a.resolve().stat().st_dev == b.parent.resolve().stat().st_dev
    except FileNotFoundError:
        return False


class Deduplicator:
    """Stateful dedup helper bound to a DB session factory and a pool dir.

    Concurrency note: SQLite (rollback-journal mode, our default) allows
    only ONE writer at a time per database file. When ``bulk-sync`` runs
    multiple chat-jobs in parallel and two of them hit ``process_file``
    simultaneously, the loser raises ``OperationalError: database is
    locked``. We serialise every dedup write through ``_write_lock`` so
    one process-wide writer is the upper bound — the bottleneck stays
    on disk I/O for the actual hash, not on SQLite contention. The
    PRAGMA-level ``busy_timeout`` set in ``src.db.session`` is a
    backup safety net for the writes happening OUTSIDE this lock
    (chat_state advance, job persistence).
    """

    def __init__(self, session_factory: SessionFactory, pool_dir: Path) -> None:
        self._session_factory = session_factory
        self._pool_dir = pool_dir
        self._pool_dir.mkdir(parents=True, exist_ok=True)
        self._write_lock = asyncio.Lock()

    async def process_file(self, target_path: Path, *, mime: str | None = None) -> DedupOutcome:
        """Dedup a single file that already exists at `target_path`.

        The file at `target_path` is hashed; on duplicate we replace it with a
        hardlink into the pool. On first sight we move it into the pool and
        hardlink back.
        """
        if not target_path.exists():
            raise FileNotFoundError(target_path)

        sha = hash_file(target_path)
        size = target_path.stat().st_size
        canonical = _pool_path(self._pool_dir, sha)

        # Lock spans the read-then-write window. Without it two parallel
        # jobs can each SELECT (miss), each INSERT, then SQLite either
        # serialises and one wins / one explodes, or hits UNIQUE on sha.
        async with self._write_lock, self._session_factory() as session:
            existing = (
                await session.execute(select(MediaFile).where(MediaFile.sha256 == sha))
            ).scalar_one_or_none()

            if existing is not None:
                saved = self._relink(target_path, Path(existing.original_path), size)
                existing.link_count += 1
                existing.bytes_saved_via_links += saved
                await session.commit()
                return DedupOutcome(
                    sha256=sha,
                    size=size,
                    was_duplicate=True,
                    bytes_saved=saved,
                    canonical_path=Path(existing.original_path),
                    final_path=target_path,
                )

            canonical.parent.mkdir(parents=True, exist_ok=True)
            self._promote_to_pool(target_path, canonical)

            row = MediaFile(
                sha256=sha,
                original_path=str(canonical),
                size_bytes=size,
                mime_type=mime,
                link_count=1,
                bytes_saved_via_links=0,
            )
            session.add(row)
            await session.commit()

        return DedupOutcome(
            sha256=sha,
            size=size,
            was_duplicate=False,
            bytes_saved=0,
            canonical_path=canonical,
            final_path=target_path,
        )

    async def process_directory(
        self,
        root: Path,
        *,
        include_globs: Iterable[str] | None = None,
    ) -> DedupBatchStats:
        """Walk `root` and dedup every file under it."""
        patterns = list(include_globs) if include_globs else ["**/*"]
        stats = DedupBatchStats()
        for pattern in patterns:
            for p in root.glob(pattern):
                if not p.is_file():
                    continue
                try:
                    outcome = await self.process_file(p)
                except (OSError, FileNotFoundError) as exc:
                    log.warning("dedup_skip", path=str(p), error=str(exc))
                    continue
                stats.total_files += 1
                if outcome.was_duplicate:
                    stats.duplicates += 1
                    stats.bytes_saved += outcome.bytes_saved
        return stats

    # ----------- private helpers -----------

    def _promote_to_pool(self, src: Path, dst_canonical: Path) -> None:
        """Move `src` into the pool as `dst_canonical` and hardlink back."""
        # Use rename when on the same volume; copy+unlink otherwise.
        if _same_volume(src, dst_canonical.parent):
            try:
                os.replace(src, dst_canonical)
            except OSError:
                shutil.copy2(src, dst_canonical)
                src.unlink(missing_ok=True)
        else:
            shutil.copy2(src, dst_canonical)
            src.unlink(missing_ok=True)

        # Link the canonical back to the expected src path so callers still
        # find the file where they put it.
        try:
            os.link(dst_canonical, src)
        except OSError as exc:
            log.warning("hardlink_failed_fallback_copy", src=str(src), dst=str(dst_canonical), error=str(exc))
            shutil.copy2(dst_canonical, src)

    def _relink(self, target: Path, canonical: Path, size: int) -> int:
        """Replace `target` with a hardlink to `canonical`. Returns bytes saved."""
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        try:
            os.link(canonical, target)
            return size
        except OSError as exc:
            log.warning(
                "hardlink_failed_fallback_copy",
                target=str(target),
                canonical=str(canonical),
                error=str(exc),
            )
            shutil.copy2(canonical, target)
            return 0
