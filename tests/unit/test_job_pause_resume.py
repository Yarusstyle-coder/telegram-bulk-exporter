"""Tests for the pause / resume / retry FSM on JobManager."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from src.db.models import JobStatus
from src.jobs.job_manager import Job, JobManager
from src.jobs.models import JobSettings, JobUpdate, JobUpdateKind


async def test_pause_then_resume_cycle() -> None:
    started = asyncio.Event()
    resumed = asyncio.Event()
    invocations = 0

    async def runner(job: Job, mgr: JobManager) -> None:
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            started.set()
            await asyncio.sleep(10)  # blocked until paused
        else:
            resumed.set()
            await mgr.emit(
                job,
                JobUpdate(
                    kind=JobUpdateKind.PROGRESS,
                    ts=datetime.now(UTC),
                    job_id=job.id,
                    percent=100.0,
                    current=1,
                    total=1,
                ),
            )

    mgr = JobManager(runner)
    job = await mgr.submit(JobSettings(chat_ids=[1]))
    await started.wait()

    assert await mgr.pause(job.id) is True
    assert job.status is JobStatus.PAUSED
    assert job.task is None

    # Pause is a no-op while already paused
    assert await mgr.pause(job.id) is False

    assert await mgr.resume(job.id) is True
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)
    assert resumed.is_set()
    assert job.status is JobStatus.SUCCEEDED


async def test_resume_a_failed_job() -> None:
    runs = 0

    async def runner(job: Job, mgr: JobManager) -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise ConnectionResetError("mock dropped connection")

    mgr = JobManager(runner)
    job = await mgr.submit(JobSettings(chat_ids=[1]))
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)
    assert job.status is JobStatus.FAILED
    assert "dropped connection" in (job.error or "")

    assert await mgr.resume(job.id) is True
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)
    assert job.status is JobStatus.SUCCEEDED
    assert runs == 2


async def test_resume_rejects_succeeded_job() -> None:
    async def runner(job: Job, mgr: JobManager) -> None:
        return

    mgr = JobManager(runner)
    job = await mgr.submit(JobSettings(chat_ids=[1]))
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)
    assert job.status is JobStatus.SUCCEEDED
    assert await mgr.resume(job.id) is False


async def test_pause_rejects_terminal_jobs() -> None:
    async def runner(job: Job, mgr: JobManager) -> None:
        return

    mgr = JobManager(runner)
    job = await mgr.submit(JobSettings(chat_ids=[1]))
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)
    assert await mgr.pause(job.id) is False
