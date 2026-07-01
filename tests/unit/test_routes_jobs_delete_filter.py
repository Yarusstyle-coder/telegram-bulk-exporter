"""Tests for the new /jobs UX:
  * ``POST /jobs/{id}/delete`` — wipes a job from in-memory + DB. Cancels
    the runner first if alive; media on disk is preserved.
  * ``GET /jobs?status=<filter>`` — server-side filter by status with
    per-group counts in the pill row.

User reported "задачи создаются в статусе paused" — actually that's the
boot-time coercion of pre-restart RUNNING rows. Delete + filters give
them a quick way to clear stale rows and zoom into actives.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import routes_auth
from src.config import Settings, reset_settings_cache
from src.db.models import Chat, ChatType, ExportJob, JobStatus
from src.db.session import create_engine, create_schema, create_session_factory
from src.main import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """App backed by a tmp DB seeded with one job in each status
    bucket so the filter pills can be exercised directly."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("PROXY_AUTO_SELECT", "false")
    # ``composition.attach_runtime`` constructs a ``TdlWrapper`` which
    # in turn asserts the binary exists — without this a checkout with no
    # ``tools/tdl/tdl.exe`` would short-circuit setup, leave
    # ``app.state.job_manager`` unset and the test page render empty.
    # We point at a non-existent stub: the JobManager itself doesn't
    # need a real binary because our ``ExportRunner.__call__`` is
    # stubbed to a no-op below.
    fake_tdl = tmp_path / "fake_tdl.exe"
    fake_tdl.write_bytes(b"")
    monkeypatch.setenv("TDL_BINARY_PATH", str(fake_tdl))
    reset_settings_cache()
    Settings().ensure_dirs()
    routes_auth.reset_auth_state()
    (tmp_path / "data" / "vault.json").write_text('{"fake": true}')

    settings_json = (
        '{"chat_ids":[1001],"media_types":["photo"],"max_file_size_bytes":null,'
        '"only_new":true,"dedup":true,"with_content":true,"threads_per_file":4,'
        '"parallel_tasks":2,"output_dir":null,"date_from":null,"date_to":null,'
        '"recent_messages":null}'
    )

    async def seed() -> None:
        eng = create_engine(tmp_path / "data" / "state.db", dek=None)
        await create_schema(eng)
        f = create_session_factory(eng)
        now = datetime.now(UTC)
        async with f() as s:
            s.add(Chat(
                id=1001, title="Alice", type=ChatType.PRIVATE,
                last_message_date=now,
            ))
            # One job per status bucket. ``upsert_job`` would be nicer
            # but we want full control of ``status`` so go raw.
            for status, age_min in [
                (JobStatus.RUNNING, 1),
                (JobStatus.PAUSED, 30),
                (JobStatus.SUCCEEDED, 120),
                (JobStatus.FAILED, 240),
                (JobStatus.CANCELLED, 360),
            ]:
                s.add(ExportJob(
                    id=str(uuid.uuid4()),
                    chat_id=1001,
                    status=status,
                    settings_json=settings_json,
                    created_at=now - timedelta(minutes=age_min),
                    started_at=now - timedelta(minutes=age_min),
                    finished_at=(
                        now - timedelta(minutes=age_min - 1)
                        if status in (
                            JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED,
                        )
                        else None
                    ),
                    bytes_saved=0, files_deduped=0,
                ))
            await s.commit()
        await eng.dispose()

    asyncio.run(seed())

    # Stub the runner so the auto-restored RUNNING row coerced to
    # PAUSED doesn't actually try to spawn tdl in tests.
    from src.jobs.exporter import ExportRunner

    async def _noop_call(self, _job, _manager):  # noqa: ANN001
        return None

    monkeypatch.setattr(ExportRunner, "__call__", _noop_call, raising=True)

    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_settings_cache()


def _forge_session(client: TestClient) -> None:
    dek = secrets.token_bytes(32)
    s = routes_auth.SESSION_STORE.create(dek, username="admin")
    s.two_fa_passed = True
    client.cookies.set("tge_session", s.token)


def _row_count(html: str) -> int:
    """Number of actual job rows in the rendered /jobs HTML.

    The page also contains a JS selector ``'[data-job-id="' + jobId +
    '"]'`` in the deleteJob() helper, so a naive ``.count`` over the
    attribute fragment overcounts by 1 on every render. Match the
    UUID-shaped value in the attribute instead."""
    import re

    return len(re.findall(r'data-job-id="[0-9a-f-]{8,}"', html))


# ---------- Status filter pills ----------


def test_jobs_default_shows_all(client: TestClient) -> None:
    """No filter → 5 jobs in the rendered list (5 statuses)."""
    _forge_session(client)
    r = client.get("/jobs")
    assert r.status_code == 200
    # Each job row carries data-job-id; count those.
    assert _row_count(r.text) == 5


def test_jobs_filter_paused(client: TestClient) -> None:
    """status=paused → only PAUSED rows. The RUNNING row that got
    coerced to PAUSED on boot-restore counts too — that's the intent
    of the filter for stale rows after a restart."""
    _forge_session(client)
    r = client.get("/jobs?status=paused")
    assert r.status_code == 200
    # Two rows: the seeded PAUSED + the RUNNING that boot-coercion
    # flipped to PAUSED.
    assert _row_count(r.text) == 2


def test_jobs_filter_succeeded(client: TestClient) -> None:
    _forge_session(client)
    r = client.get("/jobs?status=succeeded")
    assert r.status_code == 200
    assert _row_count(r.text) == 1


def test_jobs_filter_failed(client: TestClient) -> None:
    _forge_session(client)
    r = client.get("/jobs?status=failed")
    assert r.status_code == 200
    assert _row_count(r.text) == 1


def test_jobs_filter_cancelled(client: TestClient) -> None:
    _forge_session(client)
    r = client.get("/jobs?status=cancelled")
    assert r.status_code == 200
    assert _row_count(r.text) == 1


def test_jobs_filter_active_empty_after_restore(client: TestClient) -> None:
    """All RUNNING-side rows got coerced to PAUSED on restore, so
    ``status=active`` should be empty here."""
    _forge_session(client)
    r = client.get("/jobs?status=active")
    assert r.status_code == 200
    assert _row_count(r.text) == 0


def test_jobs_filter_unknown_status_falls_back_to_all(client: TestClient) -> None:
    """Garbage filter value → all jobs, not empty list. Prevents a
    typo or bookmark with old slug from leaving the user with a
    blank page."""
    _forge_session(client)
    r = client.get("/jobs?status=NOPE")
    assert r.status_code == 200
    assert _row_count(r.text) == 5


def test_jobs_pill_row_has_counts(client: TestClient) -> None:
    """Each pill carries its count as ``<span>N</span>``. We can't
    assert exact HTML for every pill without coupling to whitespace,
    so just check the pill row + at least one count rendered."""
    _forge_session(client)
    r = client.get("/jobs")
    assert "/jobs?status=all" in r.text
    assert "/jobs?status=paused" in r.text
    assert "/jobs?status=succeeded" in r.text
    assert "/jobs?status=failed" in r.text
    assert "/jobs?status=cancelled" in r.text
    assert "/jobs?status=active" in r.text


# ---------- POST /jobs/{id}/delete ----------


def test_delete_endpoint_removes_job_from_db_and_memory(
    client: TestClient, tmp_path: Path,
) -> None:
    _forge_session(client)
    # Find any restored job id from /jobs.
    r = client.get("/jobs")
    assert r.status_code == 200
    import re

    ids = re.findall(r'data-job-id="([0-9a-f-]+)"', r.text)
    assert ids, "no jobs rendered — fixture broken?"
    victim = ids[0]
    # Delete via POST.
    r2 = client.post(f"/jobs/{victim}/delete")
    assert r2.status_code == 200
    assert r2.json() == {"deleted": True}
    # Verify gone: the row no longer appears.
    r3 = client.get("/jobs")
    assert victim not in r3.text


def test_delete_endpoint_idempotent_on_missing_id(client: TestClient) -> None:
    """Deleting an unknown id returns ``{deleted: false}`` with 200
    so the UI can fire blindly without 404-handling."""
    _forge_session(client)
    r = client.post("/jobs/00000000-0000-0000-0000-000000000000/delete")
    assert r.status_code == 200
    assert r.json() == {"deleted": False}


def test_delete_endpoint_unauth_redirects_to_login(client: TestClient) -> None:
    r = client.post("/jobs/anything/delete", follow_redirects=False)
    assert r.status_code in (303, 307)


# ---------- Frontend hooks ----------


def test_jobs_page_includes_delete_js_handler(client: TestClient) -> None:
    """The deleteJob() helper that the X button calls must be in the
    rendered HTML. Pin its existence so a refactor doesn't silently
    drop the entire row-removal path."""
    _forge_session(client)
    r = client.get("/jobs")
    assert "function deleteJob(jobId, btn)" in r.text
    assert "'/jobs/' + jobId + '/delete'" in r.text


def test_each_job_row_has_x_button(client: TestClient) -> None:
    _forge_session(client)
    r = client.get("/jobs")
    # X button has ``onclick="deleteJob('<id>', this)"``. Should appear
    # for every visible row (5 with no filter).
    assert r.text.count("deleteJob(") >= 5 + 1  # 5 button calls + 1 function def


# ---------- POST /jobs/bulk-delete ----------


def test_bulk_delete_paused_removes_only_paused_rows(client: TestClient) -> None:
    """status=paused → deletes both the orig PAUSED and the RUNNING-
    coerced-to-PAUSED rows. Other statuses survive."""
    _forge_session(client)
    r = client.post("/jobs/bulk-delete?status=paused")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "paused"
    assert body["requested"] == 2
    assert len(body["deleted"]) == 2
    assert body["failed"] == []
    # Verify only 3 rows left (succeeded, failed, cancelled).
    r2 = client.get("/jobs")
    assert _row_count(r2.text) == 3


def test_bulk_delete_succeeded_only_drops_succeeded(client: TestClient) -> None:
    """Wiping succeeded history shouldn't touch the failed/cancelled
    rows so the user keeps an audit trail of what went wrong."""
    _forge_session(client)
    r = client.post("/jobs/bulk-delete?status=succeeded")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] == 1
    assert len(body["deleted"]) == 1
    # 5 - 1 = 4 left.
    r2 = client.get("/jobs")
    assert _row_count(r2.text) == 4


def test_bulk_delete_all_wipes_everything(client: TestClient) -> None:
    _forge_session(client)
    r = client.post("/jobs/bulk-delete?status=all")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "all"
    assert body["requested"] == 5
    assert len(body["deleted"]) == 5
    # /jobs is now empty.
    r2 = client.get("/jobs")
    assert _row_count(r2.text) == 0


def test_bulk_delete_active_is_noop_after_restore(client: TestClient) -> None:
    """Boot-coercion flipped RUNNING → PAUSED so there are no active
    jobs in this fixture — bulk-delete with status=active should be
    a clean no-op (requested=0, deleted=0)."""
    _forge_session(client)
    r = client.post("/jobs/bulk-delete?status=active")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["requested"] == 0
    assert body["deleted"] == []


def test_bulk_delete_unknown_status_returns_400(client: TestClient) -> None:
    """Bad filter slug must surface as 400 instead of silently
    nuking everything."""
    _forge_session(client)
    r = client.post("/jobs/bulk-delete?status=NOPE")
    assert r.status_code == 400


def test_bulk_delete_unauth_redirects_to_login(client: TestClient) -> None:
    r = client.post("/jobs/bulk-delete?status=paused", follow_redirects=False)
    assert r.status_code in (303, 307)


# ---------- Frontend hooks for bulk-delete ----------


def test_jobs_page_includes_bulk_delete_button_when_rows_present(
    client: TestClient,
) -> None:
    """When the rendered view has ≥1 row, the bulk-delete button
    must be present and labelled with the slug + count."""
    _forge_session(client)
    r = client.get("/jobs")
    assert 'id="bulk-delete-btn"' in r.text
    assert "deleteAllVisible" in r.text
    # Default filter is "all" → label "все задачи" + count 5.
    assert "все задачи" in r.text
    assert "(5)" in r.text


def test_jobs_page_hides_bulk_delete_button_when_empty(client: TestClient) -> None:
    """``status=active`` is empty in this fixture — button must not
    appear so the user doesn't click a no-op."""
    _forge_session(client)
    r = client.get("/jobs?status=active")
    assert 'id="bulk-delete-btn"' not in r.text


def test_jobs_page_includes_bulk_delete_js_handler(client: TestClient) -> None:
    _forge_session(client)
    r = client.get("/jobs")
    assert "function deleteAllVisible(btn)" in r.text
    assert "/jobs/bulk-delete?status=" in r.text
