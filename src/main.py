"""FastAPI entrypoint.

Wires everything together:

- Lifespan creates application directories and (lazily) the job manager.
- Middleware gates non-auth routes behind a valid session cookie.
- Routers are mounted in a defensive try/except so smoke tests can still
  boot if a later phase isn't fully wired yet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import get_settings
from src.logging_setup import configure_logging, get_logger

log = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"
_STATIC_DIR = Path(__file__).parent / "web" / "static"


# URL prefixes that don't require a logged-in session.
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/health",
    "/static",
    "/setup",
    "/login",
)


def _silence_proactor_connection_reset() -> None:
    """Swallow Windows ProactorEventLoop's noisy `ConnectionResetError`
    (WinError 10054) raised after a WebSocket client force-closes a TCP
    socket. The exception bubbles out of asyncio's default handler and
    can take down uvicorn — Python issue 39010 / aiohttp #4324. Safe to
    drop because the connection is already closed."""
    import asyncio
    import sys

    if sys.platform != "win32":
        return

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return

    def _handler(loop, context):  # noqa: ANN001
        exc = context.get("exception")
        msg = context.get("message", "")
        if isinstance(exc, ConnectionResetError) or "10054" in msg or "10053" in msg:
            return  # swallow
        loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.ensure_dirs()
    _silence_proactor_connection_reset()

    # Attach a plaintext-DB runtime by default so the job manager + tdl
    # wrapper are available for UI pages even before the user logs in.
    # Once the user unlocks the vault, `routes_auth` swaps in an
    # SQLCipher-backed engine via `composition.attach_runtime(app, dek=...)`.
    try:
        from src.composition import attach_runtime, detach_runtime

        await attach_runtime(app, dek=None)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("runtime_attach_skipped", error=str(exc))
        detach_runtime = None  # type: ignore[assignment]

    # Restore the user-chosen auto-lock window from user_prefs.json
    # BEFORE any session-restore work — otherwise a session restored
    # from keyring would still tick down against the stale module-level
    # default ("never lock"). Failure is non-fatal.
    try:
        from src.api.routes_auth import SESSION_STORE
        from src.auth.user_prefs import load_user_prefs

        prefs = load_user_prefs()
        SESSION_STORE.auto_lock_seconds = float(prefs.get("auto_lock_seconds", 0))
        log.info(
            "auto_lock_restored", auto_lock_seconds=SESSION_STORE.auto_lock_seconds
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("user_prefs_restore_failed", error=str(exc))

    # Restore session token + DEK from the OS secret store. Old browser
    # cookies will keep working as if the server never restarted.
    if settings.persist_sessions:
        try:
            from src.auth.persist import load as load_persisted
            from src.crypto.vault import vault_exists

            persisted = load_persisted()
            if persisted is not None and vault_exists(settings.vault_path):
                token, dek = persisted
                from src.api.routes_auth import SESSION_STORE

                SESSION_STORE.restore(
                    token=token, dek=dek, username="admin", two_fa_passed=True
                )
                log.info("session_restored_from_keyring", token=token[:6])
        except Exception as exc:  # noqa: BLE001
            log.warning("session_restore_failed", error=str(exc))

    # Migrate any token-prefixed Telethon session file to the fixed name.
    try:
        from src.api.routes_telegram import _migrate_legacy_session

        _migrate_legacy_session()
    except Exception as exc:  # noqa: BLE001
        log.warning("session_migrate_failed", error=str(exc))

    log.info("startup", host=settings.web_host, port=settings.web_port)
    try:
        yield
    finally:
        if detach_runtime is not None:
            try:
                await detach_runtime(app)
            except Exception as exc:  # pragma: no cover
                log.warning("runtime_detach_failed", error=str(exc))
        log.info("shutdown")


def _is_public_path(path: str) -> bool:
    if path == "/" or path in ("/lock", "/change-password"):
        return False
    return any(path == p or path.startswith(p + "/") for p in _PUBLIC_PREFIXES)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Telegram Bulk Exporter",
        version="0.1.0",
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.templates = templates

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    _install_friendly_error_handlers(app)
    _install_session_middleware(app)
    _mount_core_routes(app, templates)
    _mount_routers(app)
    return app


def _install_friendly_error_handlers(app: FastAPI) -> None:
    """When a browser address-bars a POST-only route (or a 404), redirect
    instead of returning a stack trace.

    JSON / API clients (Accept doesn't mention text/html) keep getting the
    raw status code so curl/HTMX behaviour is unchanged.
    """
    from fastapi.exceptions import HTTPException as _HTTPException
    from starlette.exceptions import HTTPException as _StarletteHTTPException
    from starlette.responses import RedirectResponse as _Redirect

    async def _maybe_redirect(request: Request, exc):  # noqa: ANN001
        accept = request.headers.get("accept", "")
        wants_html = "text/html" in accept
        status_code = getattr(exc, "status_code", 500)
        if wants_html and status_code in (404, 405) and request.method == "GET":
            # Redirect to the parent path, falling back to /.
            path = request.url.path.rstrip("/")
            parent = path.rsplit("/", 1)[0] or "/"
            return _Redirect(parent, status_code=303)
        # Fall through — let FastAPI render the default error response.
        from fastapi.responses import JSONResponse

        detail = getattr(exc, "detail", str(exc))
        return JSONResponse({"detail": detail}, status_code=status_code)

    app.add_exception_handler(_StarletteHTTPException, _maybe_redirect)
    app.add_exception_handler(_HTTPException, _maybe_redirect)


def _install_session_middleware(app: FastAPI) -> None:
    """Gate non-public routes behind `SESSION_STORE`.

    Users without a valid session get a 303 redirect to `/setup` (first run) or
    `/login`. WebSocket routes are not handled here — they check the cookie
    themselves if needed, and the front-end only opens them while logged in.
    """

    @app.middleware("http")
    async def session_gate(request: Request, call_next):  # noqa: ANN001
        path = request.url.path
        if _is_public_path(path):
            return await call_next(request)

        try:
            from src.api.routes_auth import SESSION_STORE
            from src.crypto.vault import vault_exists
        except Exception:
            # Auth router not mounted (early skeleton smoke); let requests through.
            return await call_next(request)

        settings = get_settings()
        if not vault_exists(settings.vault_path):
            return RedirectResponse("/setup", status_code=303)

        token = request.cookies.get(settings.session_cookie_name)
        session = SESSION_STORE.get(token) if token else None
        if session is None or not session.two_fa_passed:
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


def _mount_core_routes(app: FastAPI, templates: Jinja2Templates) -> None:
    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": "0.1.0"})

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"title": "Telegram Bulk Exporter"},
        )


def _mount_routers(app: FastAPI) -> None:
    for name in (
        "routes_auth",
        "routes_chats",
        "routes_jobs",
        "routes_avatars",
        "routes_telegram",
        "routes_proxy",
        "routes_debug",
        "routes_history",
    ):
        try:
            mod = __import__(f"src.api.{name}", fromlist=["router"])
            app.include_router(mod.router)
        except Exception as exc:  # pragma: no cover - optional in early phases
            log.warning("router_not_mounted", name=name, error=str(exc))


app = create_app()


def run() -> None:
    """Console-script entrypoint.

    NOTE on Windows: we INTENTIONALLY keep the default ProactorEventLoop
    (not Selector) because asyncio.create_subprocess_exec — which our
    tdl wrapper depends on — requires Proactor on Windows. The
    ConnectionResetError noise (WinError 10054) that Proactor emits on
    abrupt WebSocket disconnects is suppressed inside lifespan via
    `_silence_proactor_connection_reset()`.
    """
    settings = get_settings()
    uvicorn.run(
        "src.main:app",
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    run()
