"""In-process job orchestration + WebSocket broadcast.

Design
------
Each Job is an `asyncio.Task` running `_run()` on a JobRunner — the actual
work (calling tdl, dedup, state updates) is injected via a callable so the
manager stays pure orchestration.

Two data channels per job:
1. `updates: asyncio.Queue[JobUpdate]` — in-order event stream.
2. A set of subscriber queues for each live WebSocket viewer. The manager
   fans out every update to every subscriber; slow subscribers get their
   queue bounded at 256 items and are dropped on overflow.

Persistence:
- On status transitions the manager writes a row to `export_jobs`.
- Subscribers get a replay of the last 200 events when they attach (simple
  per-job ring buffer), so late viewers see progress from "almost" the start.

Cancellation:
- `cancel(job_id)` cancels the inner task; the runner is responsible for
  cleaning up subprocess handles.

Tests: see `tests/unit/test_job_manager.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.db.models import JobStatus
from src.jobs.models import JobSettings, JobUpdate, JobUpdateKind
from src.logging_setup import get_logger

if TYPE_CHECKING:
    from src.jobs.persistence import JobPersistence

log = get_logger(__name__)

_RING_SIZE = 200
_SUB_QUEUE_MAX = 256


RunnerFn = Callable[
    ["Job", "JobManager"],
    Awaitable[None],
]
"""The actual work. Injected by the composition root.

The runner MUST, at minimum, call `manager.emit(job, ...)` with a final
`COMPLETE` or `ERROR` update.
"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Job:
    id: str
    settings: JobSettings
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = field(default_factory=_utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    # Live progress snapshot for synchronous readers (/jobs HTML table).
    percent: float = 0.0
    current: int = 0
    total: int = 0
    files_deduped: int = 0
    bytes_saved: int = 0
    task: asyncio.Task[None] | None = None


class JobManager:
    """In-memory registry of active jobs with live WebSocket fan-out."""

    def __init__(
        self,
        runner: RunnerFn,
        *,
        persistence: JobPersistence | None = None,
    ) -> None:
        self._runner = runner
        self._persistence = persistence
        self._jobs: dict[str, Job] = {}
        self._ring: dict[str, deque[JobUpdate]] = {}
        self._subs: dict[str, set[asyncio.Queue[JobUpdate]]] = {}
        self._lock = asyncio.Lock()

    # -------- persistence helpers --------

    async def _persist(self, job: Job) -> None:
        """Best-effort upsert. A DB hiccup must never break a live job."""
        if self._persistence is None:
            return
        try:
            await self._persistence.upsert_job(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("job_persist_failed", job_id=job.id, error=str(exc))

    # -------- public API --------

    def list_jobs(self) -> list[Job]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def submit(self, settings: JobSettings) -> Job:
        job = Job(id=str(uuid.uuid4()), settings=settings)
        async with self._lock:
            self._jobs[job.id] = job
            self._ring[job.id] = deque(maxlen=_RING_SIZE)
            self._subs[job.id] = set()
        await self._persist(job)
        job.task = asyncio.create_task(self._safe_run(job), name=f"job-{job.id[:8]}")
        return job

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.task is None:
            return False
        if job.task.done():
            return False
        job.task.cancel()
        # Wait for the cancellation to propagate down through
        # ``ExportRunner`` → ``TdlWrapper._run_streaming`` →
        # ``await proc.wait()`` where the CancelledError handler
        # calls ``proc.kill()`` on the tdl child. Returning before
        # the await leaves the tdl process alive holding the
        # single-writer bolt-DB lock — next sync then fails with
        # "Current database is used by another process" and the user
        # sees a string of failed jobs they don't understand.
        # Swallow the final exception so the API caller always gets
        # a clean True.
        try:
            await job.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        job.status = JobStatus.CANCELLED
        job.task = None
        await self.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.STATUS,
                ts=_utcnow(),
                job_id=job.id,
                status=JobStatus.CANCELLED.value,
            ),
        )
        await self._persist(job)
        return True

    async def pause(self, job_id: str) -> bool:
        """Stop the running task gracefully and mark the job PAUSED.

        The exporter writes `chat_state.last_exported_message_id` only after
        each chat finishes successfully, so a paused job loses at most the
        in-flight chat's progress. `resume()` re-submits a fresh task with
        the same settings; `only_new=True` (default) means it picks up where
        we left off.
        """
        job = self._jobs.get(job_id)
        if job is None or job.task is None or job.task.done():
            return False
        if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.PAUSED):
            return False
        job.task.cancel()
        # Wait for the task to finish cancelling so the subprocess is gone.
        try:
            await job.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        job.status = JobStatus.PAUSED
        job.task = None
        await self.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.STATUS,
                ts=_utcnow(),
                job_id=job.id,
                status=JobStatus.PAUSED.value,
            ),
        )
        await self.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.LOG,
                ts=_utcnow(),
                job_id=job.id,
                level="info",
                message="paused — resume picks up at last successful message id",
            ),
        )
        await self._persist(job)
        return True

    async def resume(self, job_id: str) -> bool:
        """Re-spawn the runner for a PAUSED or FAILED job.

        Uses the same settings; the exporter's incremental cursor takes care
        of skipping already-downloaded messages.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status not in (JobStatus.PAUSED, JobStatus.FAILED):
            return False
        # Reset transient fields so the UI reflects a fresh run.
        job.status = JobStatus.PENDING
        job.error = None
        job.percent = 0.0
        job.current = 0
        job.total = 0
        # Restored jobs (from persistence) come in without their fan-out
        # plumbing — initialise it lazily so resume always has a place to
        # publish events.
        if job.id not in self._ring:
            self._ring[job.id] = deque(maxlen=_RING_SIZE)
        if job.id not in self._subs:
            self._subs[job.id] = set()
        await self._persist(job)
        job.task = asyncio.create_task(self._safe_run(job), name=f"job-{job.id[:8]}-resume")
        await self.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.STATUS,
                ts=_utcnow(),
                job_id=job.id,
                status=JobStatus.PENDING.value,
            ),
        )
        await self.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.LOG,
                ts=_utcnow(),
                job_id=job.id,
                level="info",
                message="resumed — continuing from last_exported_message_id",
            ),
        )
        return True

    async def delete(self, job_id: str) -> bool:
        """Remove a job from in-memory state + persistence.

        Cancels the runner task if it's still alive (so a half-finished
        tdl child is killed before the row goes away). Returns False
        when the job isn't tracked at all. Idempotent — calling delete
        on an already-removed id is a no-op that returns False.

        Note: media files on disk are NOT touched. The user keeps any
        downloaded files; deleting a job only clears the audit-trail
        row + UI entry.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return False
        # Cancel running tasks gracefully so subprocesses + WS subscribers
        # get a clean shutdown signal.
        if job.task is not None and not job.task.done():
            job.task.cancel()
            try:
                await job.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        async with self._lock:
            self._jobs.pop(job_id, None)
            self._ring.pop(job_id, None)
            self._subs.pop(job_id, None)
        if self._persistence is not None:
            try:
                await self._persistence.delete(job_id)
            except Exception as exc:  # noqa: BLE001 — non-fatal; in-memory is gone
                log.warning("job_persistence_delete_failed", job_id=job_id, error=str(exc))
        log.info("job_deleted", job_id=job_id)
        return True

    async def restore_from_persistence(self) -> int:
        """Hydrate `self._jobs` with rows that were non-terminal at boot.

        Each restored job comes back with `task=None`; the user resumes it
        explicitly via /jobs/{id}/resume (or the new /retry alias for
        FAILED). Returns the number of jobs brought back into memory.
        """
        if self._persistence is None:
            return 0
        try:
            rows = await self._persistence.load_resumable()
        except Exception as exc:  # noqa: BLE001
            log.warning("job_restore_failed", error=str(exc))
            return 0
        async with self._lock:
            for job, _settings in rows:
                if job.id in self._jobs:
                    continue  # double-call guard
                self._jobs[job.id] = job
                self._ring[job.id] = deque(maxlen=_RING_SIZE)
                self._subs[job.id] = set()
        log.info("jobs_restored", count=len(rows))
        return len(rows)

    async def emit(self, job: Job, update: JobUpdate) -> None:
        """Push one update to the ring buffer + all live subscribers."""
        # Update the in-memory snapshot so sync readers see current progress.
        if update.kind is JobUpdateKind.PROGRESS:
            if update.percent is not None:
                job.percent = update.percent
            if update.current is not None:
                job.current = update.current
            if update.total is not None:
                job.total = update.total
        elif update.kind is JobUpdateKind.STATUS and update.status:
            try:
                job.status = JobStatus(update.status)
            except ValueError:
                pass
        elif update.kind is JobUpdateKind.COMPLETE:
            job.finished_at = _utcnow()
            if update.bytes_saved is not None:
                job.bytes_saved = update.bytes_saved
            if update.files_deduped is not None:
                job.files_deduped = update.files_deduped
        elif update.kind is JobUpdateKind.ERROR:
            job.error = update.message or update.status

        self._ring[job.id].append(update)
        dead: list[asyncio.Queue[JobUpdate]] = []
        for q in list(self._subs[job.id]):
            try:
                q.put_nowait(update)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subs[job.id].discard(q)

    async def subscribe(self, job_id: str) -> AsyncIterator[JobUpdate]:
        """Async iterator yielding historical + live updates. Closes when job ends."""
        if job_id not in self._jobs:
            return
        q: asyncio.Queue[JobUpdate] = asyncio.Queue(maxsize=_SUB_QUEUE_MAX)
        for past in list(self._ring[job_id]):
            await q.put(past)
        self._subs[job_id].add(q)
        try:
            while True:
                update = await q.get()
                yield update
                if update.kind in (JobUpdateKind.COMPLETE, JobUpdateKind.ERROR):
                    break
                if self._jobs[job_id].status in (
                    JobStatus.SUCCEEDED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                ) and q.empty():
                    break
        finally:
            self._subs[job_id].discard(q)

    # -------- internal --------

    async def _safe_run(self, job: Job) -> None:
        job.started_at = _utcnow()
        job.status = JobStatus.RUNNING
        await self.emit(
            job,
            JobUpdate(
                kind=JobUpdateKind.STATUS,
                ts=_utcnow(),
                job_id=job.id,
                status=JobStatus.RUNNING.value,
            ),
        )
        await self._persist(job)
        try:
            await self._runner(job, self)
            if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
                job.status = JobStatus.SUCCEEDED
                await self.emit(
                    job,
                    JobUpdate(
                        kind=JobUpdateKind.COMPLETE,
                        ts=_utcnow(),
                        job_id=job.id,
                        status=JobStatus.SUCCEEDED.value,
                        bytes_saved=job.bytes_saved,
                        files_deduped=job.files_deduped,
                    ),
                )
                await self._persist(job)
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            await self.emit(
                job,
                JobUpdate(
                    kind=JobUpdateKind.COMPLETE,
                    ts=_utcnow(),
                    job_id=job.id,
                    status=JobStatus.CANCELLED.value,
                ),
            )
            await self._persist(job)
            raise
        except Exception as exc:  # noqa: BLE001 — we reflect the error outward
            log.exception("job_runner_failed", job_id=job.id, error=str(exc))
            job.status = JobStatus.FAILED
            job.error = str(exc)
            await self.emit(
                job,
                JobUpdate(
                    kind=JobUpdateKind.ERROR,
                    ts=_utcnow(),
                    job_id=job.id,
                    message=str(exc),
                    level="error",
                ),
            )
            await self._persist(job)
