"""On-demand history enrichment + HTML re-render + transcript viewer.

Routes:
  POST /history/{chat_id}/enrich → enrich + re-render messages.html
  GET  /history/{chat_id}/view   → serve the chat's messages.html,
                                   media is served from sibling URLs
                                   inside the same /history namespace.
  GET  /history/{chat_id}/file/{path} → static file out of the chat
                                   folder (media/, messages.html, …).

The viewer routes let the chat list double-click into a working
preview without exposing the entire export filesystem to the user.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from src.config import get_settings
from src.db.models import Chat
from src.jobs.history import render_history_html
from src.jobs.history_enrich import enrich_history
from src.logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/history", tags=["history"])


@router.post("/{chat_id}/enrich")
async def enrich_chat_history(chat_id: int, request: Request) -> JSONResponse:
    settings = get_settings()
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(503, "session factory not ready")

    # Locate the chat folder + messages.json. We mirror the slug logic
    # the exporter uses so this works on any chat that's been exported.
    title: str | None = None
    async with factory() as s:
        row = (await s.execute(select(Chat).where(Chat.id == chat_id))).scalar_one_or_none()
    if row is not None:
        title = row.title or (f"@{row.username}" if row.username else None)

    from src.jobs.exporter import _chat_slug  # local import — keep route lean

    chat_dir = settings.export_dir / _chat_slug(chat_id, title)
    history_path = chat_dir / "messages.json"
    html_path = chat_dir / "messages.html"
    if not history_path.exists():
        raise HTTPException(
            404, f"messages.json not found at {history_path} — run an export first"
        )

    # Telethon manager — try a fresh ensure() so the call works even if
    # the user reloaded the page after a server restart.
    try:
        from src.api.routes_telegram import ensure_telegram_manager

        mgr: Any | None = await ensure_telegram_manager(request)
    except Exception as exc:  # noqa: BLE001
        log.warning("history_enrich_no_manager", error=str(exc))
        mgr = None
    if mgr is None:
        raise HTTPException(503, "Telegram client not connected")

    stats = await enrich_history(
        chat_id=chat_id,
        messages_json=history_path,
        avatars_dir=settings.avatars_dir,
        manager=mgr,
    )

    # Re-render so the user sees the result immediately.
    try:
        render_history_html(
            history_path, html_path, title=title, avatars_dir=settings.avatars_dir
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("history_html_render_failed", chat_id=chat_id, error=str(exc))

    return JSONResponse(
        {
            "chat_id": chat_id,
            "total": stats.total,
            "enriched": stats.enriched,
            "me_id": stats.me_id,
            "html": str(html_path),
        }
    )


@router.get("/{chat_id}/view")
async def view_chat_html(chat_id: int, request: Request):  # noqa: ANN201
    """Open the chat in the bundled TG-Desktop-style editor.

    Returns a redirect to ``/static/tg_export/editor.html`` with
    ``?src=/history/{id}/file/messages.html`` so the editor fetches +
    auto-loads the transcript. The editor rewrites relative ``media/…``
    URLs against the absolute source URL, so attachments resolve via
    the same /history/{id}/file/ route without further server help.

    A ``?raw=1`` query param falls back to serving the raw HTML directly
    (useful for tests + curl) — relative paths are still rewritten so
    they resolve through /history/{id}/file/.
    """
    from urllib.parse import quote

    from fastapi.responses import HTMLResponse, RedirectResponse

    chat_dir = await _resolve_chat_dir(chat_id, request)
    html_path = chat_dir / "messages.html"
    if not html_path.exists():
        # Return a friendly HTML page (status 200) instead of a 404 — the
        # global ``_maybe_redirect`` handler in main.py turns 404s on
        # HTML-accepting GETs into a parent-path redirect chain that
        # ultimately lands the user on ``/``. That made double-clicking a
        # never-exported chat look like "перебрасывает на главное меню"
        # in a fresh tab. Show a usable inline page instead.
        not_ready_title: str | None = None
        factory = getattr(request.app.state, "session_factory", None)
        if factory is not None:
            async with factory() as s:
                row = (
                    await s.execute(select(Chat).where(Chat.id == chat_id))
                ).scalar_one_or_none()
            if row is not None:
                not_ready_title = row.title or (
                    f"@{row.username}" if row.username else None
                )
        safe_title = (not_ready_title or f"chat {chat_id}").replace(
            "<", "&lt;"
        ).replace(">", "&gt;")
        body = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>{safe_title} — экспорт ещё не готов</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #0f172a;
         color: #e2e8f0; padding: 4rem 2rem; max-width: 640px; margin: 0 auto; }}
  h1   {{ font-size: 1.4rem; font-weight: 600; margin: 0 0 1rem; }}
  p    {{ line-height: 1.55; color: #94a3b8; }}
  code {{ background: #1e293b; padding: 0 .25rem; border-radius: 4px; }}
  a    {{ color: #818cf8; }}
</style>
</head><body>
<h1>«{safe_title}»</h1>
<p>Этот чат ещё не экспортирован — файл <code>messages.html</code>
   ещё не сгенерирован.</p>
<p>Вернись в <a href="/chats">список чатов</a>, выбери его и нажми
   «Экспортировать» (или «Синхр.», если экспорт уже шёл). После
   завершения экспорта эта страница откроется с настоящим транскриптом.</p>
</body></html>
"""
        from fastapi.responses import HTMLResponse as _HTMLResponse

        return _HTMLResponse(body, status_code=200)

    if request.query_params.get("raw") == "1":
        body = html_path.read_text(encoding="utf-8")
        base = f"/history/{chat_id}/file/"
        body = body.replace('src="media/', f'src="{base}media/')
        body = body.replace('href="media/', f'href="{base}media/')
        return HTMLResponse(body)

    title: str | None = None
    factory = getattr(request.app.state, "session_factory", None)
    if factory is not None:
        async with factory() as s:
            row = (
                await s.execute(select(Chat).where(Chat.id == chat_id))
            ).scalar_one_or_none()
        if row is not None:
            title = row.title or (f"@{row.username}" if row.username else None)

    src_url = f"/history/{chat_id}/file/messages.html"
    target = f"/static/tg_export/editor.html?src={quote(src_url, safe='/')}"
    if title:
        target += f"&title={quote(title, safe='')}"
    return RedirectResponse(target, status_code=303)


@router.get("/{chat_id}/file/{path:path}")
async def chat_file(chat_id: int, path: str, request: Request):  # noqa: ANN201
    """Serve a single file out of the chat folder. Used by /view to
    fetch media + relative avatars referenced by messages.html.

    Path traversal is blocked: the resolved file must stay inside
    either the chat folder or the avatars dir.
    """
    settings = get_settings()
    chat_dir = await _resolve_chat_dir(chat_id, request)
    # Normalise a relative path. Reject absolute / parent-escape attempts.
    from pathlib import Path

    rel = Path(path)
    if rel.is_absolute() or any(p == ".." for p in rel.parts):
        # Allow exactly one level of `..` if it points at the avatars dir
        # (the renderer uses ../../data/avatars/<id>.jpg).
        candidate = (chat_dir / rel).resolve()
        avatars = settings.avatars_dir.resolve()
        if avatars in candidate.parents or candidate.parent == avatars:
            target = candidate
        else:
            raise HTTPException(403, "path traversal blocked")
    else:
        target = (chat_dir / rel).resolve()
        if chat_dir.resolve() not in target.parents and target != chat_dir.resolve():
            raise HTTPException(403, "outside chat folder")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"{rel} not found")
    return FileResponse(target)


async def _resolve_chat_dir(chat_id: int, request: Request):  # noqa: ANN202
    settings = get_settings()
    factory = getattr(request.app.state, "session_factory", None)
    title: str | None = None
    if factory is not None:
        async with factory() as s:
            row = (await s.execute(select(Chat).where(Chat.id == chat_id))).scalar_one_or_none()
        if row is not None:
            title = row.title or (f"@{row.username}" if row.username else None)
    from src.jobs.exporter import _chat_slug

    return settings.export_dir / _chat_slug(chat_id, title)
