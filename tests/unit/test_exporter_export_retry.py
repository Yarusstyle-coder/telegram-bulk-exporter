"""Regression: the ``tdl chat export`` phase retries transient failures.

User report (console traceback): an ``only_new`` export of @rabbitmanson
(chat id 881859567) died with ``tdl chat export … exited 1: context
deadline exceeded`` — tdl timed out mid-export under throttling. The
export phase only retried bolt-DB locks and empty-peer fallbacks, so a
single transient network timeout propagated straight to the JobManager
and marked the whole job FAILED.

The export loop now also retries transient timeouts ("context deadline
exceeded", "retry limit reached", i/o timeout, conn reset) with
exponential backoff, and sleeps + retries Telegram FloodWaits — mirroring
the ``dl`` phase. These tests pin both halves: a transient failure
recovers, and a permanently-broken export still fails loudly after the
retries are exhausted.
"""

from __future__ import annotations

import asyncio
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

CHAT_ID = 881859567


def _payload() -> dict:
    return {
        "id": CHAT_ID,
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


class _FakeTdl:
    """Minimal tdl stub: the WINDOW export fails N times (transient), then
    succeeds. The cheap tip-probe (``last_n=1``, a single-message fetch the
    real runner makes first) always succeeds — only the bulk range export is
    flaky, which is what the retry loop must recover from."""

    def __init__(self, export_failures: int) -> None:
        self._export_failures = export_failures
        self.export_calls = 0  # window range exports only
        self.probe_calls = 0
        self.dl_calls = 0

    async def chat_ls(self) -> list[dict]:
        return []

    async def chat_export(self, chat, *, output: Path, last_n=None, **_kw) -> None:  # noqa: ANN001
        if last_n is not None:
            # Tip probe — always succeeds, yields tip id 10.
            self.probe_calls += 1
            output.write_text(json.dumps(_payload()), encoding="utf-8")
            return None
        self.export_calls += 1
        if self.export_calls <= self._export_failures:
            # No structured errors → kind="unknown"; the "context deadline
            # exceeded" string is what the export loop matches as transient.
            raise TdlSubprocessError(
                argv=["tdl", "chat", "export"],
                returncode=1,
                stdout="",
                stderr="  - context deadline exceeded",
                errors=None,
            )
        output.write_text(json.dumps(_payload()), encoding="utf-8")
        return None

    async def dl(self, *, manifest, output_dir, **_kw) -> DlResult:  # noqa: ANN001
        self.dl_calls += 1
        return DlResult(files_downloaded=1, bytes_total=1234, elapsed_seconds=0.0, errors=[])


@pytest.fixture
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the export loop's exponential-backoff sleeps so tests run instantly."""

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


async def _build_runner(tmp_path: Path, tdl: _FakeTdl) -> ExportRunner:
    engine = create_engine(tmp_path / "state.db", dek=None)
    await create_schema(engine)
    sf = create_session_factory(engine)
    async with sf() as s:
        # username set → single @handle candidate, no warmup chat_ls needed.
        s.add(
            Chat(
                id=CHAT_ID,
                title="rabbitmanson",
                username="rabbitmanson",
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


async def test_export_retries_transient_failure_then_succeeds(
    tmp_path: Path, _no_backoff_sleep: None
) -> None:
    tdl = _FakeTdl(export_failures=2)
    runner = await _build_runner(tmp_path, tdl)
    mgr = JobManager(runner=runner)

    job = await mgr.submit(JobSettings(chat_ids=[CHAT_ID], only_new=False, dedup=False))
    await job.task

    # 2 transient timeouts + 1 success — the job recovers instead of failing.
    assert tdl.export_calls == 3
    assert job.status == JobStatus.SUCCEEDED


async def test_export_gives_up_after_exhausting_transient_retries(
    tmp_path: Path, _no_backoff_sleep: None
) -> None:
    tdl = _FakeTdl(export_failures=99)  # export is permanently broken
    runner = await _build_runner(tmp_path, tdl)
    mgr = JobManager(runner=runner)

    job = await mgr.submit(JobSettings(chat_ids=[CHAT_ID], only_new=False, dedup=False))
    await job.task

    # _MAX_TRANSIENT=4 → 4 export calls, then the job fails loudly.
    assert tdl.export_calls == 4
    assert job.status == JobStatus.FAILED
    assert tdl.dl_calls == 0  # never reached the download phase
