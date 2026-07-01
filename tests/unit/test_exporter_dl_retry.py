"""Regression: the ``tdl dl`` phase retries transient failures.

User report: "не экспортируется личный чат Папа" (chat id 298263017).
The download phase exited 1 with ``retry limit reached after 5 attempts``
during a bulk parallel export — classic Telegram throttling. The bare
``self._tdl.dl(...)`` call propagated the first non-zero exit straight to
the JobManager, marking the whole job FAILED, so a single throttled chat
in a bulk run silently never exported.

The exporter now wraps ``dl`` in :func:`with_retry` (exponential backoff)
the same way the ``chat_export`` phase already retries. ``--skip-same``
makes each retry cheap. These tests pin both halves of that contract:
transient failures recover, and a permanently-broken download still fails
loudly after the retries are exhausted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.db.models import Chat, ChatType, JobStatus
from src.db.session import create_engine, create_schema, create_session_factory
from src.jobs.exporter import ExportRunner
from src.jobs.job_manager import JobManager
from src.jobs.models import JobSettings
from src.services.tdl_types import DlResult
from src.services.tdl_wrapper import TdlSubprocessError


class _FakeTdl:
    """Minimal tdl stub: chat_export always succeeds, dl fails N times."""

    def __init__(self, dl_failures: int) -> None:
        self._dl_failures = dl_failures
        self.dl_calls = 0
        self.export_calls = 0

    async def chat_ls(self) -> list[dict]:
        return []

    async def chat_export(self, chat, *, output: Path, **_kw) -> None:  # noqa: ANN001
        self.export_calls += 1
        payload = {
            "id": 298263017,
            "type": "private",
            "messages": [
                {
                    "ID": 10,
                    "Date": 1700000000,
                    "Message": "hi",
                    "Media": {"Name": "pic.jpg", "Size": 1234},
                },
            ],
        }
        output.write_text(json.dumps(payload), encoding="utf-8")
        return None

    async def dl(self, *, manifest, output_dir, **_kw) -> DlResult:  # noqa: ANN001
        self.dl_calls += 1
        if self.dl_calls <= self._dl_failures:
            # No structured errors → tdl_retry classifies as kind="unknown",
            # which is retriable. Mirrors the real bulk-export symptom.
            raise TdlSubprocessError(
                argv=["tdl", "dl"],
                returncode=1,
                stdout="",
                stderr="  - retry limit reached after 5 attempts",
                errors=None,
            )
        return DlResult(files_downloaded=1, bytes_total=1234, elapsed_seconds=0.0, errors=[])


@pytest.fixture
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the exponential-backoff sleeps so the test runs instantly."""

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("src.services.tdl_retry.asyncio.sleep", _no_sleep)


async def _build_runner(tmp_path: Path, tdl: _FakeTdl) -> ExportRunner:
    engine = create_engine(tmp_path / "state.db", dek=None)
    await create_schema(engine)
    sf = create_session_factory(engine)
    async with sf() as s:
        s.add(
            Chat(
                id=298263017,
                title="Папа",
                username=None,
                type=ChatType.PRIVATE,
            )
        )
        await s.commit()
    return ExportRunner(
        session_factory=sf,
        tdl_wrapper=tdl,
        deduplicator=None,  # dedup disabled via settings → never invoked
        export_dir=tmp_path / "exports",
        avatars_dir=None,  # skips the Telethon enrich branch
        telegram_manager_provider=None,
    )


async def test_dl_retries_transient_failure_then_succeeds(
    tmp_path: Path, _no_backoff_sleep: None
) -> None:
    tdl = _FakeTdl(dl_failures=2)
    runner = await _build_runner(tmp_path, tdl)
    mgr = JobManager(runner=runner)

    job = await mgr.submit(JobSettings(chat_ids=[298263017], only_new=False, dedup=False))
    await job.task

    # 2 throttled exits + 1 success — the job recovers instead of failing.
    assert tdl.dl_calls == 3
    assert job.status == JobStatus.SUCCEEDED


async def test_dl_gives_up_after_exhausting_retries(
    tmp_path: Path, _no_backoff_sleep: None
) -> None:
    tdl = _FakeTdl(dl_failures=99)  # download is permanently broken
    runner = await _build_runner(tmp_path, tdl)
    mgr = JobManager(runner=runner)

    job = await mgr.submit(JobSettings(chat_ids=[298263017], only_new=False, dedup=False))
    await job.task

    # with_retry max_attempts=4 → 4 dl calls, then the job fails loudly.
    assert tdl.dl_calls == 4
    assert job.status == JobStatus.FAILED
