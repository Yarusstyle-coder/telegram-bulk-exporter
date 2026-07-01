"""id-range chunking of the chat-export phase.

A full-history ``tdl chat export`` of a large chat runs for many minutes as a
single, non-resumable call — one ``context deadline exceeded`` loses the whole
pass. The runner now sweeps the message-id space in ``_EXPORT_CHUNK_ID_SPAN``
windows, persisting ``ChatState.export_cursor_message_id`` after each completed
window so a crash resumes mid-sweep instead of restarting.

These tests pin: the windowing maths, per-window cursor checkpointing, resume
from a live cursor, watermark advance + cursor clear on completion, the
"already at the tip" no-op, and that ``recent_messages`` stays a single
un-chunked window.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from src.db.models import Chat, ChatState, ChatType, JobStatus
from src.db.session import create_engine, create_schema, create_session_factory
from src.jobs.exporter import ExportRunner
from src.jobs.job_manager import JobManager
from src.jobs.models import JobSettings
from src.services.tdl_types import DlResult
from src.services.tdl_wrapper import TdlSubprocessError

CHAT_ID = 555000111


def _msg(mid: int, *, media: bool = True) -> dict:
    m: dict = {"ID": mid, "Date": 1_700_000_000 + mid, "Message": f"m{mid}"}
    if media:
        m["Media"] = {"Name": f"f{mid}.jpg", "Size": 100 + mid}
    return m


class _ChunkTdl:
    """Fake tdl that honours the ``-i lo,hi`` id range so windowing is
    observable. ``last_n=1`` is the tip probe; ``last_n>1`` is recent-mode.
    """

    def __init__(self, *, tip: int | None, media_ids: list[int], fail_from: int | None = None) -> None:
        self.tip = tip
        self.media_ids = sorted(media_ids)
        self.fail_from = fail_from  # a window from_id that should hard-fail
        self.windows: list[tuple[int, int]] = []  # (from_id, to_id) range exports
        self.last_n_calls: list[int] = []
        self.probe_calls = 0
        self.dl_calls = 0

    async def chat_ls(self) -> list[dict]:
        return []

    async def chat_export(  # noqa: ANN001
        self, chat, *, output: Path, from_id=None, to_id=None, last_n=None, **_kw
    ) -> None:
        if last_n is not None:
            self.last_n_calls.append(last_n)
            if last_n == 1:
                self.probe_calls += 1
                msgs = [_msg(self.tip, media=False)] if self.tip is not None else []
            else:
                ids = self.media_ids[-last_n:]
                msgs = [_msg(i) for i in ids]
            output.write_text(
                json.dumps({"id": CHAT_ID, "type": "private", "messages": msgs}),
                encoding="utf-8",
            )
            return None

        # id-range window export
        self.windows.append((from_id, to_id))
        if self.fail_from is not None and from_id == self.fail_from:
            # Non-retriable (not lock/transient/flood/empty-peer) → fails fast.
            raise TdlSubprocessError(
                argv=["tdl", "chat", "export"],
                returncode=1,
                stdout="",
                stderr="  - boom: permanent window failure",
                errors=None,
            )
        ids = [i for i in self.media_ids if from_id <= i <= to_id]
        output.write_text(
            json.dumps({"id": CHAT_ID, "type": "private", "messages": [_msg(i) for i in ids]}),
            encoding="utf-8",
        )
        return None

    async def dl(self, *, manifest, output_dir, **_kw) -> DlResult:  # noqa: ANN001
        self.dl_calls += 1
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
        msgs = data.get("messages") or []
        n = 0
        for m in msgs:
            name = m.get("Media", {}).get("Name", f"{m['ID']}.bin")
            (Path(output_dir) / f"{m['ID']}_{name}").write_text("x", encoding="utf-8")
            n += 1
        return DlResult(files_downloaded=n, bytes_total=n * 10, elapsed_seconds=0.0, errors=[])


@pytest.fixture
def _tiny_span(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the chunk span so a handful of ids spans several windows."""
    monkeypatch.setattr("src.jobs.exporter._EXPORT_CHUNK_ID_SPAN", 10)


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _z(_s: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _z)


async def _runner(tmp_path: Path, tdl: _ChunkTdl, *, state: ChatState | None = None):
    engine = create_engine(tmp_path / "state.db", dek=None)
    await create_schema(engine)
    sf = create_session_factory(engine)
    async with sf() as s:
        s.add(Chat(id=CHAT_ID, title="Big", username="bigchat", type=ChatType.PRIVATE))
        await s.flush()
        if state is not None:
            state.chat_id = CHAT_ID
            s.add(state)
        await s.commit()
    runner = ExportRunner(
        session_factory=sf,
        tdl_wrapper=tdl,
        deduplicator=None,
        export_dir=tmp_path / "exports",
        avatars_dir=None,
        telegram_manager_provider=None,
    )
    return runner, sf


async def _state(sf) -> ChatState | None:
    async with sf() as s:
        return (
            await s.execute(select(ChatState).where(ChatState.chat_id == CHAT_ID))
        ).scalar_one_or_none()


async def test_full_sweep_chunks_into_windows(tmp_path: Path, _tiny_span, _no_sleep) -> None:
    tdl = _ChunkTdl(tip=25, media_ids=[5, 15, 23])
    runner, sf = await _runner(tmp_path, tdl)
    mgr = JobManager(runner=runner)

    job = await mgr.submit(JobSettings(chat_ids=[CHAT_ID], only_new=False, dedup=False))
    await job.task

    assert job.status == JobStatus.SUCCEEDED
    # lo=0, tip=25, span=10 → three windows.
    assert tdl.windows == [(0, 9), (10, 19), (20, 25)]
    assert tdl.dl_calls == 3
    # one file per media id, downloaded across the windows
    media_dir = next((tmp_path / "exports").glob("chat_*/media"))
    assert sum(1 for _ in media_dir.iterdir()) == 3
    # watermark advanced to the tip, cursor cleared
    st = await _state(sf)
    assert st.last_exported_message_id == 25
    assert st.export_cursor_message_id is None


async def test_resume_from_cursor_skips_done_windows(tmp_path: Path, _tiny_span, _no_sleep) -> None:
    # An interrupted sweep left the cursor at 19 (windows [0..9],[10..19] done).
    tdl = _ChunkTdl(tip=25, media_ids=[5, 15, 23])
    runner, sf = await _runner(
        tmp_path, tdl, state=ChatState(export_cursor_message_id=19)
    )
    mgr = JobManager(runner=runner)

    job = await mgr.submit(JobSettings(chat_ids=[CHAT_ID], only_new=True, dedup=False))
    await job.task

    assert job.status == JobStatus.SUCCEEDED
    # resume at cursor+1=20 → only the final window re-runs; earlier ones skipped.
    assert tdl.windows == [(20, 25)]
    st = await _state(sf)
    assert st.last_exported_message_id == 25
    assert st.export_cursor_message_id is None


async def test_failed_window_leaves_cursor_at_last_good(tmp_path: Path, _tiny_span, _no_sleep) -> None:
    tdl = _ChunkTdl(tip=25, media_ids=[5, 15, 23], fail_from=10)
    runner, sf = await _runner(tmp_path, tdl)
    mgr = JobManager(runner=runner)

    job = await mgr.submit(JobSettings(chat_ids=[CHAT_ID], only_new=False, dedup=False))
    await job.task

    assert job.status == JobStatus.FAILED
    # window [0..9] committed its cursor; [10..19] failed before advancing.
    st = await _state(sf)
    assert st.export_cursor_message_id == 9
    assert st.last_exported_message_id is None  # watermark never advanced


async def test_already_at_tip_is_noop(tmp_path: Path, _tiny_span, _no_sleep) -> None:
    tdl = _ChunkTdl(tip=25, media_ids=[5, 15, 23])
    runner, sf = await _runner(
        tmp_path,
        tdl,
        state=ChatState(last_exported_message_id=25),
    )
    mgr = JobManager(runner=runner)

    job = await mgr.submit(JobSettings(chat_ids=[CHAT_ID], only_new=True, dedup=False))
    await job.task

    assert job.status == JobStatus.SUCCEEDED
    # lo = 26 > tip 25 → probe only, no range exports, no downloads.
    assert tdl.windows == []
    assert tdl.dl_calls == 0
    assert tdl.probe_calls >= 1


async def test_recent_messages_is_single_unchunked_window(tmp_path: Path, _tiny_span, _no_sleep) -> None:
    tdl = _ChunkTdl(tip=25, media_ids=[5, 15, 23])
    runner, sf = await _runner(tmp_path, tdl)
    mgr = JobManager(runner=runner)

    job = await mgr.submit(
        JobSettings(chat_ids=[CHAT_ID], only_new=False, dedup=False, recent_messages=5)
    )
    await job.task

    assert job.status == JobStatus.SUCCEEDED
    # recent mode: no id-range windows, no tip probe — one last_n request.
    assert tdl.windows == []
    assert tdl.probe_calls == 0
    assert tdl.last_n_calls == [5]
    assert tdl.dl_calls == 1
