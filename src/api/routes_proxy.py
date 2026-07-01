"""HTTP routes for the proxy pool admin UI.

- GET  /proxy            → HTML dashboard with all entries + ping cards.
- POST /proxy            → form fields: `url`, optional `label`. Adds entry,
                           re-renders the dashboard (HTMX-friendly fragment).
- POST /proxy/test       → re-measure all entries; HTMX returns the table.
- POST /proxy/select     → form field: `url` — set this one as active.
- POST /proxy/auto       → run measure_and_pick_best.
- POST /proxy/{url}/delete → remove the entry (URL is form-encoded).

The pool lives at `request.app.state.proxy_pool`. Composition root attaches
it during lifespan.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote_plus

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/proxy", tags=["proxy"])


def _templates(request: Request) -> Any:
    return request.app.state.templates


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "proxy_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="proxy pool not available")
    return pool


@router.get("", response_class=HTMLResponse)
async def proxy_dashboard(request: Request) -> HTMLResponse:
    pool = _pool(request)
    return _templates(request).TemplateResponse(
        request,
        "proxy.html",
        {
            "title": "Прокси",
            "entries": pool.list_entries(),
            "active_url": pool.active_url(),
        },
    )


@router.get("/fragment", response_class=HTMLResponse)
async def proxy_fragment(request: Request) -> HTMLResponse:
    pool = _pool(request)
    return _templates(request).TemplateResponse(
        request,
        "_proxy_fragment.html",
        {
            "entries": pool.list_entries(),
            "active_url": pool.active_url(),
        },
    )


@router.post("", response_class=HTMLResponse)
async def proxy_add(
    request: Request,
    url: str = Form(...),
    label: str | None = Form(None),
) -> HTMLResponse:
    pool = _pool(request)
    try:
        await pool.add(url.strip(), label=(label or "").strip() or None)
    except ValueError as exc:
        return _templates(request).TemplateResponse(
            request,
            "_proxy_fragment.html",
            {
                "entries": pool.list_entries(),
                "active_url": pool.active_url(),
                "error": str(exc),
            },
        )
    return await proxy_fragment(request)


@router.post("/test", response_class=HTMLResponse)
async def proxy_test_all(request: Request) -> HTMLResponse:
    pool = _pool(request)
    await pool.measure_all()
    return await proxy_fragment(request)


@router.post("/auto", response_class=HTMLResponse)
async def proxy_auto_pick(request: Request) -> HTMLResponse:
    pool = _pool(request)
    winner = await pool.measure_and_pick_best()
    log.info("proxy_auto_pick", url=winner.url if winner else None)
    return await proxy_fragment(request)


@router.post("/select", response_class=HTMLResponse)
async def proxy_select(request: Request, url: str = Form(...)) -> HTMLResponse:
    pool = _pool(request)
    # Validate the URL belongs to the pool — refuse arbitrary externals.
    pool_urls = {e.url for e in pool.list_entries()}
    if url not in pool_urls:
        raise HTTPException(status_code=404, detail="proxy not in pool")
    await pool.set_active(url)
    return await proxy_fragment(request)


@router.post("/disable", response_class=HTMLResponse)
async def proxy_disable(request: Request) -> HTMLResponse:
    """Drop the active proxy: Telethon and tdl will go direct.

    Useful when VPN already routes your traffic and the listed proxies are
    fake-TLS MTProxy servers that Telethon doesn't speak.
    """
    pool = _pool(request)
    await pool.set_active(None)
    log.info("proxy_disabled")
    return await proxy_fragment(request)


@router.post("/delete", response_class=HTMLResponse)
async def proxy_delete(request: Request, url: str = Form(...)) -> HTMLResponse:
    pool = _pool(request)
    raw = unquote_plus(url)
    await pool.remove(raw)
    return await proxy_fragment(request)
