"""Unit tests for the JobManager fan-out + lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from src.db.models import JobStatus
from src.jobs.job_manager import Job, JobManager
from src.jobs.models import JobSettings, JobUpdate, JobUpdateKind


def _progress(job_id: str, pct: float) -> JobUpdate:
    return JobUpdate(
        kind=JobUpdateKind.PROGRESS,
        ts=datetime.now(UTC),
        job_id=job_id,
        percent=pct,
        current=int(pct),
        total=100,
    )


async def test_submit_runs_to_success() -> None:
    async def runner(job: Job, mgr: JobManager) -> None:
        for pct in (10, 50, 90):
            await mgr.emit(job, _progress(job.id, float(pct)))
            await asyncio.sleep(0)

    mgr = JobManager(runner)
    job = await mgr.submit(JobSettings(chat_ids=[1]))
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)
    assert job.status is JobStatus.SUCCEEDED
    assert job.percent == 90


async def test_subscribe_replays_ring_and_streams_live() -> None:
    ready = asyncio.Event()

    async def runner(job: Job, mgr: JobManager) -> None:
        await mgr.emit(job, _progress(job.id, 10.0))
        await mgr.emit(job, _progress(job.id, 20.0))
        ready.set()
        await asyncio.sleep(0.05)
        await mgr.emit(job, _progress(job.id, 80.0))

    mgr = JobManager(runner)
    job = await mgr.submit(JobSettings(chat_ids=[1]))
    await ready.wait()

    stream = mgr.subscribe(job.id)
    received: list[float] = []
    async for upd in stream:
        if upd.kind is JobUpdateKind.PROGRESS and upd.percent is not None:
            received.append(upd.percent)
        if upd.kind is JobUpdateKind.COMPLETE:
            break

    # 10 + 20 replayed, 80 live, plus final complete events
    assert received[:2] == [10.0, 20.0]
    assert 80.0 in received


async def test_runner_error_becomes_failed_status() -> None:
    async def runner(job: Job, mgr: JobManager) -> None:
        raise RuntimeError("boom")

    mgr = JobManager(runner)
    job = await mgr.submit(JobSettings(chat_ids=[1]))
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)
    assert job.status is JobStatus.FAILED
    assert "boom" in (job.error or "")


async def test_cancel_transitions_to_cancelled() -> None:
    ready = asyncio.Event()

    async def runner(job: Job, mgr: JobManager) -> None:
        ready.set()
        # Sleep long enough to get cancelled
        await asyncio.sleep(10)

    mgr = JobManager(runner)
    job = await mgr.submit(JobSettings(chat_ids=[1]))
    await ready.wait()
    assert await mgr.cancel(job.id)
    # cancel() now awaits the underlying task before returning, so the
    # cancellation has already been processed by the time the API call
    # comes back. The manager nulls out the task handle to signal
    # "fully unwound, no tdl child left over" — see comment in
    # JobManager.cancel for the bolt-DB lock issue this prevents.
    assert job.task is None
    assert job.status is JobStatus.CANCELLED


async def test_cancel_waits_for_runner_cleanup() -> None:
    """The fix: cancel() must await the inner task so the runner's
    ``finally`` block (where in production we call ``proc.kill()`` on
    the tdl subprocess) actually runs BEFORE the HTTP response goes
    back to the caller. Previously cancel() returned immediately and
    the runner could still be torn down for arbitrary time afterward
    — leaving a tdl child holding the bolt-DB lock and the next
    sync would fail with "Current database is used by another
    process"."""
    runner_finally_ran = asyncio.Event()
    runner_started = asyncio.Event()

    async def runner(job: Job, mgr: JobManager) -> None:
        runner_started.set()
        try:
            await asyncio.sleep(10)
        finally:
            # Yield to the event loop so the cleanup is observable
            # *only* after the runner has fully unwound.
            await asyncio.sleep(0)
            runner_finally_ran.set()

    mgr = JobManager(runner)
    job = await mgr.submit(JobSettings(chat_ids=[1]))
    await runner_started.wait()
    assert not runner_finally_ran.is_set()
    assert await mgr.cancel(job.id)
    # The critical assertion: cleanup has happened by the time the
    # API call returns. No "wait a bit then check" loops needed.
    assert runner_finally_ran.is_set()


async def test_get_and_list() -> None:
    async def runner(job: Job, mgr: JobManager) -> None:
        await asyncio.sleep(0)

    mgr = JobManager(runner)
    j1 = await mgr.submit(JobSettings(chat_ids=[1]))
    j2 = await mgr.submit(JobSettings(chat_ids=[2]))

    assert {j.id for j in mgr.list_jobs()} == {j1.id, j2.id}
    assert mgr.get(j1.id) is j1
    assert mgr.get("nope") is None


async def test_progress_snapshot_updates_job() -> None:
    async def runner(job: Job, mgr: JobManager) -> None:
        await mgr.emit(job, _progress(job.id, 37.0))

    mgr = JobManager(runner)
    job = await mgr.submit(JobSettings(chat_ids=[1]))
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)
    assert job.percent == 37.0
    assert job.current == 37
    assert job.total == 100
