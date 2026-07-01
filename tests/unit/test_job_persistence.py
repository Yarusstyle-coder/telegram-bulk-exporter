"""Tests for JobPersistence + JobManager.restore_from_persistence().

Covers:
- A submitted job creates an `export_jobs` row.
- A runner that emits ERROR persists FAILED + error_message.
- A pause writes status=paused; reload restores it as PAUSED.
- A row that was status=running on disk gets coerced to PAUSED on load.
- `persistence=None` keeps the manager fully operational (no DB writes).
- Restored FAILED job retains FAILED status (so the UI shows Retry).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select

from src.db.models import ExportJob, JobStatus
from src.db.session import create_engine, create_schema, create_session_factory
from src.jobs.job_manager import Job, JobManager
from src.jobs.models import JobSettings
from src.jobs.persistence import JobPersistence


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator:
    eng = create_engine(tmp_path / "state.db", dek=None)
    await create_schema(eng)
    factory = create_session_factory(eng)
    yield factory
    await eng.dispose()


@pytest.fixture
async def chat_seed(session_factory) -> int:
    """Insert a row in `chats` so the FK on `export_jobs` is happy."""
    from src.db.models import Chat, ChatType

    chat_id = 9001
    async with session_factory() as s:
        s.add(Chat(id=chat_id, title="seed", type=ChatType.PRIVATE))
        await s.commit()
    return chat_id


# ---------- direct persistence tests ----------


async def test_upsert_creates_row(session_factory, chat_seed) -> None:
    p = JobPersistence(session_factory)
    job = Job(id=str(uuid.uuid4()), settings=JobSettings(chat_ids=[chat_seed]))
    await p.upsert_job(job)

    async with session_factory() as s:
        rows = (await s.execute(select(ExportJob))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == job.id
    assert rows[0].chat_id == chat_seed
    assert rows[0].status is JobStatus.PENDING


async def test_upsert_updates_existing_row(session_factory, chat_seed) -> None:
    p = JobPersistence(session_factory)
    job = Job(id=str(uuid.uuid4()), settings=JobSettings(chat_ids=[chat_seed]))
    await p.upsert_job(job)
    job.status = JobStatus.RUNNING
    job.bytes_saved = 4242
    await p.upsert_job(job)

    async with session_factory() as s:
        rows = (await s.execute(select(ExportJob))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status is JobStatus.RUNNING
    assert rows[0].bytes_saved == 4242


async def test_upsert_skips_when_chat_ids_empty(session_factory) -> None:
    p = JobPersistence(session_factory)
    job = Job(id=str(uuid.uuid4()), settings=JobSettings(chat_ids=[]))
    await p.upsert_job(job)
    async with session_factory() as s:
        rows = (await s.execute(select(ExportJob))).scalars().all()
    assert rows == []


async def test_load_resumable_coerces_running_to_paused(
    session_factory, chat_seed
) -> None:
    p = JobPersistence(session_factory)
    job = Job(
        id=str(uuid.uuid4()),
        settings=JobSettings(chat_ids=[chat_seed]),
        status=JobStatus.RUNNING,
    )
    await p.upsert_job(job)

    restored = await p.load_resumable()
    assert len(restored) == 1
    restored_job, _ = restored[0]
    assert restored_job.status is JobStatus.PAUSED


async def test_load_resumable_keeps_failed(session_factory, chat_seed) -> None:
    p = JobPersistence(session_factory)
    job = Job(
        id=str(uuid.uuid4()),
        settings=JobSettings(chat_ids=[chat_seed]),
        status=JobStatus.FAILED,
        error="connection reset",
    )
    await p.upsert_job(job)

    restored = await p.load_resumable()
    assert len(restored) == 1
    restored_job, _ = restored[0]
    assert restored_job.status is JobStatus.FAILED
    assert restored_job.error == "connection reset"


async def test_load_resumable_keeps_terminal_for_history(session_factory, chat_seed) -> None:
    """Terminal SUCCEEDED/CANCELLED rows come back too so the user keeps a
    recent /jobs history visible after a restart. Status is preserved
    as-is — they get no resume/retry button in the UI by virtue of
    `JobManager.resume()` rejecting non-(PAUSED|FAILED) statuses."""
    p = JobPersistence(session_factory)
    for st in (JobStatus.SUCCEEDED, JobStatus.CANCELLED):
        job = Job(
            id=str(uuid.uuid4()),
            settings=JobSettings(chat_ids=[chat_seed]),
            status=st,
        )
        await p.upsert_job(job)
    restored = await p.load_resumable()
    statuses = sorted(j.status.value for j, _ in restored)
    assert statuses == [JobStatus.CANCELLED.value, JobStatus.SUCCEEDED.value]


async def test_mark_finished(session_factory, chat_seed) -> None:
    p = JobPersistence(session_factory)
    job = Job(
        id=str(uuid.uuid4()),
        settings=JobSettings(chat_ids=[chat_seed]),
        status=JobStatus.RUNNING,
    )
    await p.upsert_job(job)
    await p.mark_finished(job.id, JobStatus.FAILED, error="timeout")

    async with session_factory() as s:
        row = (await s.execute(select(ExportJob))).scalars().one()
    assert row.status is JobStatus.FAILED
    assert row.error_message == "timeout"


# ---------- JobManager integration ----------


async def test_submit_persists_row(session_factory, chat_seed) -> None:
    p = JobPersistence(session_factory)

    async def runner(job: Job, mgr: JobManager) -> None:
        await asyncio.sleep(0)

    mgr = JobManager(runner=runner, persistence=p)
    job = await mgr.submit(JobSettings(chat_ids=[chat_seed]))
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)

    async with session_factory() as s:
        row = (await s.execute(select(ExportJob))).scalars().one()
    assert row.id == job.id
    assert row.status is JobStatus.SUCCEEDED


async def test_runner_error_persists_failed(session_factory, chat_seed) -> None:
    p = JobPersistence(session_factory)

    async def runner(job: Job, mgr: JobManager) -> None:
        raise ConnectionResetError("dropped")

    mgr = JobManager(runner=runner, persistence=p)
    job = await mgr.submit(JobSettings(chat_ids=[chat_seed]))
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)
    assert job.status is JobStatus.FAILED

    async with session_factory() as s:
        row = (await s.execute(select(ExportJob))).scalars().one()
    assert row.status is JobStatus.FAILED
    assert "dropped" in (row.error_message or "")


async def test_restore_from_persistence_into_fresh_manager(
    session_factory, chat_seed
) -> None:
    p = JobPersistence(session_factory)

    # First manager — submit and force a FAILED job.
    async def boom(job: Job, mgr: JobManager) -> None:
        raise RuntimeError("first run")

    mgr1 = JobManager(runner=boom, persistence=p)
    job_failed = await mgr1.submit(JobSettings(chat_ids=[chat_seed]))
    assert job_failed.task is not None
    await asyncio.wait_for(job_failed.task, timeout=2)
    assert job_failed.status is JobStatus.FAILED

    # Manually persist a "still running" row to simulate a crash mid-flight.
    crashed = Job(
        id=str(uuid.uuid4()),
        settings=JobSettings(chat_ids=[chat_seed]),
        status=JobStatus.RUNNING,
    )
    await p.upsert_job(crashed)

    # Fresh manager — restore should hydrate both.
    async def noop(job: Job, mgr: JobManager) -> None:
        return

    mgr2 = JobManager(runner=noop, persistence=p)
    count = await mgr2.restore_from_persistence()
    assert count == 2

    by_id = {j.id: j for j in mgr2.list_jobs()}
    assert by_id[job_failed.id].status is JobStatus.FAILED
    assert by_id[crashed.id].status is JobStatus.PAUSED  # coerced
    # Restored jobs come back without an attached task — user resumes manually.
    for j in mgr2.list_jobs():
        assert j.task is None


async def test_resume_a_restored_failed_job(session_factory, chat_seed) -> None:
    p = JobPersistence(session_factory)
    runs = 0

    async def flaky(job: Job, mgr: JobManager) -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("first run")

    mgr1 = JobManager(runner=flaky, persistence=p)
    j = await mgr1.submit(JobSettings(chat_ids=[chat_seed]))
    assert j.task is not None
    await asyncio.wait_for(j.task, timeout=2)
    assert j.status is JobStatus.FAILED

    # New manager — restore, then resume the FAILED job.
    mgr2 = JobManager(runner=flaky, persistence=p)
    await mgr2.restore_from_persistence()
    assert await mgr2.resume(j.id) is True
    restored = mgr2.get(j.id)
    assert restored is not None and restored.task is not None
    await asyncio.wait_for(restored.task, timeout=2)
    assert restored.status is JobStatus.SUCCEEDED
    assert runs == 2


async def test_persistence_none_is_graceful() -> None:
    """Passing persistence=None must keep the existing API fully working."""

    async def runner(job: Job, mgr: JobManager) -> None:
        await asyncio.sleep(0)

    mgr = JobManager(runner=runner)  # no persistence kwarg
    assert await mgr.restore_from_persistence() == 0

    job = await mgr.submit(JobSettings(chat_ids=[1]))
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)
    assert job.status is JobStatus.SUCCEEDED


async def test_db_hiccup_does_not_break_submit(session_factory, chat_seed) -> None:
    """A persistence error must not propagate up to the user-facing job."""

    class BrokenPersistence:
        async def upsert_job(self, _job: Job) -> None:
            raise RuntimeError("disk full")

    async def runner(job: Job, mgr: JobManager) -> None:
        await asyncio.sleep(0)

    mgr = JobManager(runner=runner, persistence=BrokenPersistence())  # type: ignore[arg-type]
    job = await mgr.submit(JobSettings(chat_ids=[chat_seed]))
    assert job.task is not None
    await asyncio.wait_for(job.task, timeout=2)
    assert job.status is JobStatus.SUCCEEDED
