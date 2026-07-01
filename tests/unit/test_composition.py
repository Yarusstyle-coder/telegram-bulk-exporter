"""Integration-ish test: composition root attaches runtime onto app.state."""

from __future__ import annotations

from fastapi import FastAPI

from src.composition import attach_runtime, detach_runtime
from src.jobs.job_manager import JobManager
from src.services.deduplicator import Deduplicator


async def test_attach_detach_runtime(tmp_settings) -> None:  # noqa: ARG001
    app = FastAPI()
    await attach_runtime(app, dek=None)
    assert hasattr(app.state, "db_engine")
    assert hasattr(app.state, "session_factory")
    assert isinstance(app.state.job_manager, JobManager)
    assert isinstance(app.state.deduplicator, Deduplicator)
    await detach_runtime(app)
    assert not hasattr(app.state, "db_engine")
    assert not hasattr(app.state, "job_manager")


async def test_attach_runtime_is_idempotent(tmp_settings) -> None:  # noqa: ARG001
    app = FastAPI()
    await attach_runtime(app, dek=None)
    first = app.state.db_engine
    await attach_runtime(app, dek=None)
    # New engine replaces the first cleanly; no exceptions.
    assert app.state.db_engine is not first
    await detach_runtime(app)
