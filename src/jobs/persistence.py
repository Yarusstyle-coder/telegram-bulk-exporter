"""Persist active jobs across server restarts.

`JobManager` is in-memory: an `asyncio.Task` registry. When the server
crashes mid-export, every record of "this job was running" is gone — the
user has no way to resume from the watermark in `chat_state`.

This module mirrors the in-memory `Job` snapshot into the existing
`export_jobs` table on every status transition. On boot,
`load_resumable()` reads back rows whose status was non-terminal and
`JobManager.restore_from_persistence()` re-creates `Job` instances
**without** spawning tasks — the user clicks "Возобновить" / "Повторить"
to actually re-run them.

Caveat: the `ExportJob` schema has a single-FK `chat_id`. We persist the
**first** chat id from `JobSettings.chat_ids`. Multi-chat jobs replay
correctly (the full `chat_ids` list is in `settings_json`); only the FK
points at one chat.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.db.models import ExportJob, JobStatus
from src.db.session import transaction
from src.jobs.models import JobSettings
from src.logging_setup import get_logger

if TYPE_CHECKING:
    from src.db.session import SessionFactory
    from src.jobs.job_manager import Job

log = get_logger(__name__)


# Statuses we want to bring back on boot. RUNNING / EXPORTING /
# DOWNLOADING / DEDUPING are coerced to PAUSED on load — the runtime
# crashed, so treat them as paused; the user resumes with one click.
# SUCCEEDED / CANCELLED come back as-is so the user keeps a recent
# history visible on /jobs (no resume/retry buttons get rendered
# for them, see jobs.html). Older runs fall off via the LIMIT in
# load_resumable so memory stays bounded.
_NON_TERMINAL = (
    JobStatus.RUNNING,
    JobStatus.EXPORTING,
    JobStatus.DOWNLOADING,
    JobStatus.DEDUPING,
    JobStatus.PAUSED,
    JobStatus.FAILED,
    JobStatus.SUCCEEDED,
    JobStatus.CANCELLED,
)
_COERCE_TO_PAUSED = {
    JobStatus.RUNNING,
    JobStatus.EXPORTING,
    JobStatus.DOWNLOADING,
    JobStatus.DEDUPING,
}
# How many historical jobs to bring back on boot. Keeps /jobs useful
# without ballooning memory after months of exports.
_RESTORE_LIMIT = 50


class JobPersistence:
    """Mirrors in-memory `Job` snapshots into `export_jobs`."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._factory = session_factory

    async def upsert_job(self, job: Job) -> None:
        """Create or update the `export_jobs` row for `job`.

        No-op when `settings.chat_ids` is empty — the schema requires a
        non-null FK. We log a debug breadcrumb so the silence is visible.
        """
        if not job.settings.chat_ids:
            log.debug("persistence_skip_empty_chat_ids", job_id=job.id)
            return

        chat_id = job.settings.chat_ids[0]
        settings_json = job.settings.model_dump_json()

        # SQLite UPSERT keyed on the primary key. Avoids a separate
        # SELECT-then-INSERT-or-UPDATE round trip.
        stmt = sqlite_insert(ExportJob).values(
            id=job.id,
            chat_id=chat_id,
            status=job.status,
            settings_json=settings_json,
            bytes_saved=job.bytes_saved,
            files_deduped=job.files_deduped,
            error_message=job.error,
            started_at=job.started_at,
            finished_at=job.finished_at,
            created_at=job.created_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ExportJob.id],
            set_={
                "status": stmt.excluded.status,
                "settings_json": stmt.excluded.settings_json,
                "bytes_saved": stmt.excluded.bytes_saved,
                "files_deduped": stmt.excluded.files_deduped,
                "error_message": stmt.excluded.error_message,
                "started_at": stmt.excluded.started_at,
                "finished_at": stmt.excluded.finished_at,
            },
        )
        async with self._factory() as session, transaction(session):
            await session.execute(stmt)

    async def delete(self, job_id: str) -> bool:
        """Drop the export_jobs row. Idempotent: a missing row returns
        False rather than raising, so the caller can treat
        already-deleted ids as no-ops.

        Media on disk and any chat_states rows are NOT touched — only
        the audit-trail row for this specific job."""
        async with self._factory() as session, transaction(session):
            row = await session.get(ExportJob, job_id)
            if row is None:
                return False
            await session.delete(row)
        return True

    async def mark_finished(
        self,
        job_id: str,
        status: JobStatus,
        error: str | None = None,
    ) -> None:
        """Convenience helper for terminal transitions.

        Used when the manager wants to flag a job done without holding the
        full Job object handy (e.g. inside a hook handler).
        """
        async with self._factory() as session, transaction(session):
            row = await session.get(ExportJob, job_id)
            if row is None:
                return
            row.status = status
            if error is not None:
                row.error_message = error

    async def load_resumable(self) -> list[tuple[Job, JobSettings]]:
        """Return Job snapshots for non-terminal rows.

        RUNNING/EXPORTING/DOWNLOADING/DEDUPING rows are coerced to PAUSED
        — the runtime crashed mid-flight, the user expects "this is paused
        and I can resume it". FAILED stays FAILED so the UI can show
        "Повторить" with the original error.
        """
        # Local import keeps the module-level cycle (job_manager imports
        # persistence to type-annotate, persistence imports Job at use-time).
        from src.jobs.job_manager import Job

        async with self._factory() as session:
            stmt = (
                select(ExportJob)
                .where(ExportJob.status.in_(_NON_TERMINAL))
                .order_by(ExportJob.created_at.desc())
                .limit(_RESTORE_LIMIT)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        out: list[tuple[Job, JobSettings]] = []
        for row in rows:
            try:
                settings = JobSettings.model_validate_json(row.settings_json)
            except Exception as exc:  # noqa: BLE001
                # Corrupt settings_json — surface in logs but don't crash boot.
                log.warning(
                    "persistence_skip_unparseable_settings",
                    job_id=row.id,
                    error=str(exc),
                )
                continue

            status = (
                JobStatus.PAUSED if row.status in _COERCE_TO_PAUSED else row.status
            )
            job = Job(
                id=row.id,
                settings=settings,
                status=status,
                created_at=row.created_at,
                started_at=row.started_at,
                finished_at=row.finished_at,
                error=row.error_message,
                bytes_saved=row.bytes_saved or 0,
                files_deduped=row.files_deduped or 0,
            )
            out.append((job, settings))
        return out
