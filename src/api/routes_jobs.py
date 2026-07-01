"""Job HTTP routes + WebSocket live progress.

Routes:
- GET  /jobs                 → HTML list of jobs with live progress bars.
- POST /jobs                 → create a job from the modal form (JSON or form).
- GET  /jobs/{job_id}        → detail page with full log pane.
- POST /jobs/{job_id}/cancel → cancel a running job.
- WS   /jobs/{job_id}/stream → JSON frames of JobUpdate for that job.

Uses `app.state.job_manager` as the JobManager singleton. If the manager is
not present (e.g. during very early smoke tests before lifespan wires it),
routes respond with a friendly empty state.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from src.jobs.models import JobSettings
from src.logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _templates(request: Request) -> Any:
    return request.app.state.templates


def _job_manager(request: Request) -> Any | None:
    return getattr(request.app.state, "job_manager", None)


@router.get("", response_class=HTMLResponse)
async def list_jobs(
    request: Request,
    mgr: Any | None = Depends(_job_manager),
    status: str = "all",
) -> HTMLResponse:
    """Render /jobs. Defensive: a single bad Job in memory shouldn't
    take the whole page down with a 500. We always sort by a safe
    fallback timestamp and resolve chat labels best-effort.

    ``status`` query param filters by status group:
      * ``all`` — everything (default)
      * ``active`` — running / exporting / downloading / deduping / pending
      * ``paused`` — paused only
      * ``succeeded`` / ``failed`` / ``cancelled`` — exact status
    """
    raw_jobs = mgr.list_jobs() if mgr else []
    # Stable sort: newest first by created_at; jobs with a missing
    # timestamp get pushed to the bottom rather than crashing the page.
    from datetime import UTC, datetime  # noqa: I001 — local to the route

    _epoch = datetime.fromtimestamp(0, UTC)
    try:
        jobs = sorted(
            raw_jobs,
            key=lambda j: getattr(j, "created_at", None) or _epoch,
            reverse=True,
        )
    except Exception as exc:  # noqa: BLE001 — never let sort fail the page
        log.warning("list_jobs_sort_failed", error=str(exc))
        jobs = list(raw_jobs)

    try:
        chat_labels = await _resolve_chat_labels(request, jobs)
    except Exception as exc:  # noqa: BLE001
        log.warning("list_jobs_resolve_labels_failed", error=str(exc))
        chat_labels = {}

    # Status filter — applied AFTER label resolution so counts in the
    # pills (computed below) reflect the full list, not just the
    # filtered subset. ``_ACTIVE_STATUSES`` matches what the
    # JobPersistence treats as "active mid-flight".
    _ACTIVE_STATUSES = {"running", "exporting", "downloading", "deduping", "pending"}
    status_filter = (status or "all").lower().strip()
    if status_filter == "active":
        visible = [j for j in jobs if getattr(j, "status", None) and j.status.value in _ACTIVE_STATUSES]
    elif status_filter in ("paused", "succeeded", "failed", "cancelled"):
        visible = [j for j in jobs if getattr(j, "status", None) and j.status.value == status_filter]
    else:
        visible = jobs
        status_filter = "all"

    # Per-status counts for the pill badges. Empty status groups still
    # get a 0 so the pill row layout stays stable.
    counts: dict[str, int] = {
        "all": len(jobs),
        "active": 0,
        "paused": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
    }
    for j in jobs:
        st = getattr(getattr(j, "status", None), "value", "")
        if st in _ACTIVE_STATUSES:
            counts["active"] += 1
        elif st in counts:
            counts[st] += 1

    return _templates(request).TemplateResponse(
        request,
        "jobs.html",
        {
            "title": "Задачи",
            "jobs": visible,
            "chat_labels": chat_labels,
            "status_filter": status_filter,
            "status_counts": counts,
        },
    )


async def _resolve_chat_labels(request: Request, jobs: list[Any]) -> dict[int, str]:
    """Map chat_id → 'Title (@username)' for every chat referenced by any job.

    Single SELECT IN; result lives only as long as this request. The
    template falls back to the bare numeric id when a row is missing
    (e.g. the chat got deleted from `chats` after an export).
    """
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return {}
    ids: set[int] = set()
    for j in jobs:
        for cid in j.settings.chat_ids or []:
            ids.add(cid)
    if not ids:
        return {}
    try:
        from sqlalchemy import select

        from src.db.models import Chat

        async with factory() as s:
            rows = (await s.execute(select(Chat).where(Chat.id.in_(ids)))).scalars().all()
        out: dict[int, str] = {}
        for c in rows:
            label = c.title or (f"@{c.username}" if c.username else str(c.id))
            if c.username and c.title:
                label = f"{c.title} (@{c.username})"
            out[c.id] = label
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("resolve_chat_labels_failed", error=str(exc))
        return {}


@router.post("")
async def create_job(
    request: Request,
    mgr: Any | None = Depends(_job_manager),
):
    """Create a new export job from the modal's JSON payload.

    All known failure modes return a JSON body with a human-readable
    `detail` so the UI can show the real error instead of an opaque
    "Internal Server Error". Unhandled paths log the full traceback at
    server side for diagnosis.
    """
    if mgr is None:
        raise HTTPException(status_code=503, detail="Job manager not available")
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("create_job_bad_json", error=str(exc))
        return JSONResponse({"detail": f"Bad JSON payload: {exc}"}, status_code=400)

    try:
        settings = JobSettings(**payload)
    except ValidationError as exc:
        # Surface field-level Pydantic errors to the user.
        log.info("create_job_validation_error", errors=exc.errors())
        return JSONResponse(
            {
                "detail": "Параметры задачи невалидны",
                "errors": exc.errors(),
            },
            status_code=422,
        )

    if not settings.chat_ids:
        return JSONResponse(
            {"detail": "Не выбран ни один чат для экспорта"},
            status_code=400,
        )

    try:
        job = await mgr.submit(settings)
    except Exception as exc:  # noqa: BLE001
        log.exception("create_job_submit_failed", error=str(exc))
        return JSONResponse(
            {"detail": f"Не удалось создать задачу: {type(exc).__name__}: {exc}"},
            status_code=500,
        )
    return {"job_id": job.id, "status": job.status.value}


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_detail(
    request: Request,
    job_id: str,
    mgr: Any | None = Depends(_job_manager),
) -> HTMLResponse:
    job = mgr.get(job_id) if mgr else None
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _templates(request).TemplateResponse(
        request,
        "job_detail.html",
        {"title": f"Задача {job_id[:8]}", "job": job},
    )


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    mgr: Any | None = Depends(_job_manager),
):
    if mgr is None:
        raise HTTPException(status_code=503, detail="Job manager not available")
    ok = await mgr.cancel(job_id)
    return {"cancelled": ok}


@router.post("/{job_id}/delete")
async def delete_job(
    job_id: str,
    mgr: Any | None = Depends(_job_manager),
):
    """Remove the job from memory + persistence.

    Cancels the runner task if it's still alive (so any tdl child
    gets killed before the row vanishes). Media on disk is preserved
    — only the audit-trail row goes away. Idempotent: deleting an
    unknown id returns ``{"deleted": false}`` with HTTP 200, not 404,
    so the UI can fire the request without worrying about race
    conditions with another tab.
    """
    if mgr is None:
        raise HTTPException(status_code=503, detail="Job manager not available")
    ok = await mgr.delete(job_id)
    return {"deleted": ok}


# The set of statuses we treat as "mid-flight". Mirrors the same set in
# ``list_jobs`` above + ``JobPersistence`` so the /jobs pill labelled
# "Активные" and a bulk-delete ``?status=active`` match exactly.
_ACTIVE_STATUSES = {"running", "exporting", "downloading", "deduping", "pending"}


@router.post("/bulk-delete")
async def bulk_delete_jobs(
    status: str = "active",
    mgr: Any | None = Depends(_job_manager),
):
    """Cancel + delete every job matching ``status``.

    Same filter slugs as ``GET /jobs?status=`` (``all``, ``active``,
    ``paused``, ``succeeded``, ``failed``, ``cancelled``). The default
    of ``active`` is deliberate — a one-click "wipe" must not silently
    nuke the user's historical succeeded runs unless they explicitly
    ask for it via ``?status=all``.

    Each target goes through ``JobManager.delete`` which cancels the
    runner first if it's still alive, so tdl children are killed
    cleanly. Media on disk and chat_state watermarks are NOT touched
    — only the audit-trail rows go away. The handler is best-effort:
    a per-job failure is recorded in ``failed`` but doesn't abort the
    batch, so the user gets as much cleanup as possible in one shot.
    """
    if mgr is None:
        raise HTTPException(status_code=503, detail="Job manager not available")

    sf = (status or "all").lower().strip()
    jobs = mgr.list_jobs()
    if sf == "active":
        targets = [j for j in jobs if j.status.value in _ACTIVE_STATUSES]
    elif sf in ("paused", "succeeded", "failed", "cancelled"):
        targets = [j for j in jobs if j.status.value == sf]
    elif sf == "all":
        targets = list(jobs)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown status filter: {sf!r}")

    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    for j in targets:
        try:
            ok = await mgr.delete(j.id)
        except Exception as exc:  # noqa: BLE001 — keep going for the rest
            log.exception("bulk_delete_job_failed", job_id=j.id, error=str(exc))
            failed.append({"job_id": j.id, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if ok:
            deleted.append(j.id)
        else:
            failed.append({"job_id": j.id, "reason": "delete returned False"})
    log.info(
        "bulk_delete_done",
        status=sf,
        requested=len(targets),
        deleted=len(deleted),
        failed=len(failed),
    )
    return {
        "deleted": deleted,
        "failed": failed,
        "status": sf,
        "requested": len(targets),
    }


@router.post("/{job_id}/pause")
async def pause_job(
    job_id: str,
    mgr: Any | None = Depends(_job_manager),
):
    if mgr is None:
        raise HTTPException(status_code=503, detail="Job manager not available")
    ok = await mgr.pause(job_id)
    return {"paused": ok}


@router.post("/{job_id}/resume")
async def resume_job(
    job_id: str,
    mgr: Any | None = Depends(_job_manager),
):
    if mgr is None:
        raise HTTPException(status_code=503, detail="Job manager not available")
    ok = await mgr.resume(job_id)
    return {"resumed": ok}


@router.post("/{job_id}/retry")
async def retry_job(
    job_id: str,
    mgr: Any | None = Depends(_job_manager),
):
    """Alias for /resume targeted at FAILED jobs.

    Functionally identical — `JobManager.resume()` already accepts both
    PAUSED and FAILED — but having a distinct verb in the access log
    makes failure-retry traffic easier to grep.
    """
    if mgr is None:
        raise HTTPException(status_code=503, detail="Job manager not available")
    ok = await mgr.resume(job_id)
    return {"retried": ok}


@router.websocket("/{job_id}/stream")
async def job_stream(websocket: WebSocket, job_id: str) -> None:
    """WebSocket that yields JSON JobUpdate frames until the job ends."""
    await websocket.accept()
    mgr = getattr(websocket.app.state, "job_manager", None)
    if mgr is None:
        await websocket.send_json({"error": "Job manager not available"})
        await websocket.close()
        return

    try:
        async for update in mgr.subscribe(job_id):
            await websocket.send_json(update.model_dump(mode="json"))
    except WebSocketDisconnect:
        log.info("ws_client_disconnected", job_id=job_id)
    except Exception as exc:  # noqa: BLE001 — tell the client and close
        log.exception("ws_stream_error", job_id=job_id, error=str(exc))
        try:
            await websocket.send_json({"error": str(exc)})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
