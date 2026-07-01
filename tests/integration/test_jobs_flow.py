"""End-to-end flow: create a job through POST /jobs, stream WS updates.

The runner we swap in is a tiny in-process stub — we're validating wiring,
not tdl behaviour. The real tdl path is covered elsewhere.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.db.models import JobStatus
from src.jobs.job_manager import Job, JobManager
from src.jobs.models import JobUpdate, JobUpdateKind


def _attach_stub_manager(app: FastAPI) -> JobManager:
    async def runner(job: Job, mgr: JobManager) -> None:
        for pct in (25.0, 50.0, 75.0):
            await mgr.emit(
                job,
                JobUpdate(
                    kind=JobUpdateKind.PROGRESS,
                    ts=datetime.now(UTC),
                    job_id=job.id,
                    percent=pct,
                    current=int(pct),
                    total=100,
                ),
            )
            await asyncio.sleep(0.01)

    mgr = JobManager(runner=runner)
    app.state.job_manager = mgr
    return mgr


def _make_app() -> FastAPI:
    from src.main import create_app

    app = create_app()
    return app


def _bypass_auth(monkeypatch) -> None:
    """Force the middleware to treat every request as authenticated."""
    import src.main

    monkeypatch.setattr(src.main, "_is_public_path", lambda _p: True)


def test_post_jobs_creates_and_completes(monkeypatch) -> None:
    _bypass_auth(monkeypatch)
    app = _make_app()
    client = TestClient(app)
    with client:
        # Replace the real runner AFTER lifespan has attached the real one.
        _attach_stub_manager(app)
        r = client.post(
            "/jobs",
            json={"chat_ids": [42], "media_types": ["photo"]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] in ("pending", "running")
        job_id = body["job_id"]

        # Poll for completion via GET /jobs (job runs on the TestClient loop).
        import time

        mgr = app.state.job_manager
        deadline = time.time() + 5
        while time.time() < deadline:
            job = mgr.get(job_id)
            if job is not None and job.status in (
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                break
            time.sleep(0.05)
        job = mgr.get(job_id)
        assert job is not None and job.status is JobStatus.SUCCEEDED

        r = client.get("/jobs")
        assert r.status_code == 200
        assert job_id[:8] in r.text


def test_websocket_streams_progress(monkeypatch) -> None:
    _bypass_auth(monkeypatch)
    app = _make_app()
    client = TestClient(app)
    with client:
        _attach_stub_manager(app)
        r = client.post("/jobs", json={"chat_ids": [7], "media_types": ["photo"]})
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        with client.websocket_connect(f"/jobs/{job_id}/stream") as ws:
            seen_progress: list[float] = []
            while True:
                try:
                    frame = json.loads(ws.receive_text())
                except Exception:
                    break
                if frame.get("kind") == "progress" and frame.get("percent") is not None:
                    seen_progress.append(float(frame["percent"]))
                if frame.get("kind") == "complete":
                    break
            assert len(seen_progress) >= 2


def test_chats_fragment_renders_empty(tmp_path, monkeypatch) -> None:
    """Render with an isolated empty DB so the dev's real data/ doesn't leak in."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    from src.config import reset_settings_cache

    reset_settings_cache()
    _bypass_auth(monkeypatch)
    app = _make_app()
    client = TestClient(app)
    with client:
        r = client.get("/chats/fragment")
        assert r.status_code == 200
        # Fresh DB → no checkbox rows.
        assert 'data-chat-id="' not in r.text
    reset_settings_cache()
