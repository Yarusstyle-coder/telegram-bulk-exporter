"""Telegram auth UI.

Drives the user through the Telethon login flow:

    /auth/telegram             → dashboard (status + "connect" button)
    /auth/telegram/credentials → POST api_id + api_hash (encrypted at rest)
    /auth/telegram/start       → POST phone  → renders code form
    /auth/telegram/code        → POST SMS/app code → maybe 2FA form
    /auth/telegram/password    → POST 2FA password → /chats
    /auth/telegram/disconnect  → POST: close client, nuke session, drop manager

State that survives between steps (the ``phone_code_hash``) lives inside a
single ``TelegramSessionManager`` instance. One manager per logged-in session
is cached in ``app.state.telegram_pending`` keyed by session token. When the
flow completes or the user disconnects, the entry is removed.
"""

from __future__ import annotations

import base64
import contextlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.telegram_service import (
    load_api_credentials,
    load_phone,
    save_api_credentials,
    save_phone,
)
from src.auth.session import Session
from src.config import get_settings
from src.logging_setup import get_logger
from src.telegram.telethon_client import (
    AuthStep,
    TelegramAuthError,
    TelegramSessionManager,
)

log = get_logger(__name__)

router = APIRouter(prefix="/auth/telegram", tags=["telegram"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def require_unlocked(request: Request) -> Session:
    """Return the fully-unlocked :class:`Session` or 303-redirect to ``/login``.

    We read ``SESSION_STORE`` lazily to avoid import-time coupling to the auth
    router. The redirect is raised as an HTTP exception so dependents don't
    have to type-narrow a union.
    """
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)

    # Lazy import keeps routes_auth a one-way dependency.
    from src.api.routes_auth import SESSION_STORE

    session = SESSION_STORE.get(token) if token else None
    if session is None or not session.two_fa_passed:
        # FastAPI treats a raised HTTPException with a 3xx oddly; using an
        # inner exception class that we catch in an exception handler would
        # be heavier than a tiny custom exception. Instead, we re-raise
        # a specialised exception and convert in each route below.
        raise _NeedsLogin()
    SESSION_STORE.touch(session.token)
    return session


class _NeedsLogin(Exception):
    """Raised by :func:`require_unlocked` when the user is not logged in."""


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


def _templates(request: Request) -> Any:
    return request.app.state.templates


def _session_factory(request: Request) -> Any | None:
    return getattr(request.app.state, "session_factory", None)


def _pending_map(request: Request) -> dict[str, TelegramSessionManager]:
    """Per-app cache of in-flight managers keyed by session token."""
    mp: dict[str, TelegramSessionManager] | None = getattr(
        request.app.state, "telegram_pending", None
    )
    if mp is None:
        mp = {}
        request.app.state.telegram_pending = mp
    return mp


def _session_file_path(token: str | None = None) -> Path:
    """Where the encrypted Telethon ``StringSession`` is cached on disk.

    Uses a fixed filename so the Telegram session survives across UI logins
    (every /login creates a fresh UI session token; we don't want to lose
    the underlying Telegram auth every time). The legacy token-prefixed file
    is migrated transparently — see _migrate_legacy_session().
    """
    settings = get_settings()
    return settings.sessions_dir / "telegram.tgsess"


def _migrate_legacy_session() -> None:
    """If an old <token>.tgsess file exists from before the fixed-name change,
    rename the most recent one to telegram.tgsess so the user keeps their auth.
    Called lazily on dashboard render."""
    settings = get_settings()
    fixed = settings.sessions_dir / "telegram.tgsess"
    if fixed.exists():
        return
    candidates = sorted(
        (
            p
            for p in settings.sessions_dir.glob("*.tgsess")
            if p.name != "telegram.tgsess"
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return
    try:
        candidates[0].rename(fixed)
        log.info("telegram_session_file_migrated", from_=candidates[0].name, to=fixed.name)
        for stale in candidates[1:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError as exc:
        log.warning("telegram_session_migrate_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Flash cookie helpers
# ---------------------------------------------------------------------------

_FLASH_COOKIE = "tge_flash"


def _set_flash(resp: Any, kind: str, message: str) -> None:
    # kind is either "ok" or "error". We base64-encode the message so the
    # cookie stays within latin-1 (Set-Cookie bytes) even for Cyrillic text.
    msg = message[:200].encode("utf-8")
    payload = f"{kind}|{base64.urlsafe_b64encode(msg).decode('ascii')}"
    resp.set_cookie(
        _FLASH_COOKIE,
        payload,
        max_age=30,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )


def _pop_flash(request: Request) -> tuple[str, str] | None:
    raw = request.cookies.get(_FLASH_COOKIE)
    if not raw or "|" not in raw:
        return None
    kind, _, encoded = raw.partition("|")
    try:
        message = base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return kind, message


def _clear_flash(resp: Any) -> None:
    resp.delete_cookie(_FLASH_COOKIE, path="/")


def _kickoff_avatar_resume(request: Request, manager: Any) -> None:
    """Run the avatar reconciliation pass once per process.

    Sticks an `avatars_resumed` flag on `app.state` so subsequent ensure_*
    calls don't re-trigger the scan. Errors are swallowed — a broken avatar
    pass must never break Telegram-aware routes.
    """
    if getattr(request.app.state, "avatars_resumed", False):
        return
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return
    try:
        import asyncio

        from src.services.avatar_resumer import AvatarResumer

        settings = get_settings()
        resumer = AvatarResumer(factory, settings.avatars_dir)
        request.app.state.avatars_resumed = True
        task = asyncio.create_task(
            resumer.scan_and_resume(manager),
            name="avatar-resume",
        )
        request.app.state.avatar_resume_task = task
    except Exception as exc:  # noqa: BLE001
        log.warning("avatar_resume_kickoff_failed", error=str(exc))


def _kickoff_new_message_watcher(request: Request, manager: Any) -> None:
    """Subscribe the manager to Telethon NewMessage events so
    ``chats.last_message_date`` updates in real time.

    Without this, the "stale" badge on /chats only flips when the user
    manually clicks "Обновить список" — so a chat that received fresh
    DMs since the last refresh stays falsely "synced" in the UI. The
    handler is idempotent (guarded by a flag on app.state and a flag
    on the client object inside the manager), so repeated ensure_*
    calls don't pile up duplicate writes.
    """
    if getattr(request.app.state, "new_message_watcher_started", False):
        return
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return
    try:
        import asyncio

        async def _start() -> None:
            try:
                await manager.start_new_message_watcher(factory)
            except Exception as exc:  # noqa: BLE001
                log.warning("new_message_watcher_start_failed", error=str(exc))

        request.app.state.new_message_watcher_started = True
        task = asyncio.create_task(_start(), name="tg-new-message-watcher")
        request.app.state.new_message_watcher_task = task
    except Exception as exc:  # noqa: BLE001
        log.warning("new_message_watcher_kickoff_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Helpers that talk to app state
# ---------------------------------------------------------------------------


async def ensure_telegram_manager(
    request: Request,
    session: Session | None = None,
    creds: tuple[int, str] | None = None,
) -> Any | None:
    """Ensure ``app.state.telegram_manager`` is alive and authorised.

    Idempotent. Safe to call from any route. If the manager is missing or
    broken, attempts to rebuild it from the encrypted session file +
    cached api credentials. Returns the live manager or None.
    """
    factory = _session_factory(request)

    # Resolve session if the caller didn't pass one in.
    if session is None:
        from src.api.routes_auth import SESSION_STORE

        token = request.cookies.get(get_settings().session_cookie_name)
        session = SESSION_STORE.get(token) if token else None
        if session is None:
            return None

    # Resolve creds if not given.
    if creds is None and factory is not None:
        try:
            creds = await load_api_credentials(factory, bytes(session.dek))
        except Exception:  # noqa: BLE001
            creds = None

    if creds is None or not _session_file_path().exists():
        return getattr(request.app.state, "telegram_manager", None)

    existing = getattr(request.app.state, "telegram_manager", None)
    if existing is not None:
        # Probe — drop a half-broken handle (e.g. previous bad-proxy connect).
        try:
            await existing.is_authorized()
            _kickoff_avatar_resume(request, existing)
            _kickoff_new_message_watcher(request, existing)
            return existing
        except Exception as exc:  # noqa: BLE001
            log.info("telegram_manager_dropped_broken", error=str(exc))
            with contextlib.suppress(Exception):
                await existing.close()
            if hasattr(request.app.state, "telegram_manager"):
                delattr(request.app.state, "telegram_manager")

    try:
        mgr = _build_manager(session, creds[0], creds[1], request=request)
        ok = await mgr.is_authorized()
        request.app.state.telegram_manager = mgr
        log.info("telegram_manager_auto_restored", authorized=ok)
        if ok:
            _kickoff_avatar_resume(request, mgr)
            _kickoff_new_message_watcher(request, mgr)
        return mgr
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram_manager_auto_restore_failed", error=str(exc))
        pool = getattr(request.app.state, "proxy_pool", None)
        if pool is not None and pool.active_url():
            msg = str(exc).lower()
            if "mtproxy" in msg or "16 bytes" in msg or "proxy closed" in msg:
                await pool.set_active(None)
                log.info("telegram_disabled_broken_proxy")
        return None


async def _manager_status(request: Request) -> tuple[bool, bool]:
    """Return ``(has_manager, is_authorized)`` without constructing new state."""
    mgr: TelegramSessionManager | None = getattr(
        request.app.state, "telegram_manager", None
    )
    if mgr is None:
        return False, False
    try:
        ok = await mgr.is_authorized()
    except Exception as exc:  # noqa: BLE001 — surface as "not authorized"
        log.warning("telegram_status_probe_failed", error=str(exc))
        return True, False
    return True, bool(ok)


async def _render_dashboard(
    request: Request,
    session: Session,
    *,
    error: str | None = None,
    success: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    factory = _session_factory(request)
    creds = None
    phone = None
    if factory is not None:
        try:
            creds = await load_api_credentials(factory, bytes(session.dek))
        except Exception as exc:  # noqa: BLE001 — bad DEK or missing row
            log.warning("load_api_credentials_failed", error=str(exc))
            creds = None
        try:
            phone = await load_phone(factory, bytes(session.dek))
        except Exception as exc:  # noqa: BLE001
            log.warning("load_phone_failed", error=str(exc))
            phone = None

        # First-run convenience: if .env has TELEGRAM_API_ID + TELEGRAM_API_HASH
        # but the encrypted DB has nothing yet, persist the env values now so
        # the user doesn't have to retype them. They live encrypted-at-rest
        # afterwards and survive .env deletion.
        if creds is None:
            settings = get_settings()
            if settings.telegram_api_id and settings.telegram_api_hash:
                try:
                    await save_api_credentials(
                        factory,
                        bytes(session.dek),
                        settings.telegram_api_id,
                        settings.telegram_api_hash,
                    )
                    creds = (settings.telegram_api_id, settings.telegram_api_hash)
                    log.info(
                        "telegram_api_credentials_seeded_from_env",
                        api_id=settings.telegram_api_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("telegram_env_seed_failed", error=str(exc))

    # Migrate any legacy per-token session file to the fixed filename so a
    # fresh UI login keeps the underlying Telegram auth.
    _migrate_legacy_session()

    await ensure_telegram_manager(request, session, creds)

    has_manager, is_authorized = await _manager_status(request)

    # Probe whether tdl is authorised (independent of Telethon).
    tdl_ready = False
    try:
        tdl_mgr = _get_or_create_tdl_login(request)
        tdl_ready = tdl_mgr._has_existing_session()  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        log.warning("tdl_status_probe_failed", error=str(exc))

    flash = _pop_flash(request)
    if flash and not error and not success:
        kind, msg = flash
        if kind == "error":
            error = msg
        else:
            success = msg

    resp = _templates(request).TemplateResponse(
        request,
        "telegram_dashboard.html",
        {
            "title": "Telegram",
            "credentials_configured": creds is not None,
            "api_id_masked": creds[0] if creds else None,
            "phone": phone,
            "has_manager": has_manager,
            "is_authorized": is_authorized,
            "tdl_ready": tdl_ready,
            "error": error,
            "success": success,
        },
        status_code=status_code,
    )
    _clear_flash(resp)
    return resp


def _build_manager(
    session: Session,
    api_id: int,
    api_hash: str,
    *,
    request: Request | None = None,
) -> TelegramSessionManager:
    settings = get_settings()
    settings.ensure_dirs()
    session_path = _session_file_path()

    # Prefer the live ProxyPool's active URL (auto-picked at startup).
    # Fall back to the legacy `settings.proxy` env var for compatibility.
    proxy_url: str | None = None
    if request is not None:
        pool = getattr(request.app.state, "proxy_pool", None)
        if pool is not None:
            proxy_url = pool.active_url()
    if not proxy_url:
        proxy_url = settings.proxy

    return TelegramSessionManager(
        api_id=api_id,
        api_hash=api_hash,
        session_path=session_path,
        dek=bytes(session.dek),
        proxy=proxy_url,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def dashboard_get(request: Request) -> Any:
    try:
        session = require_unlocked(request)
    except _NeedsLogin:
        return _redirect_login()
    return await _render_dashboard(request, session)


# Friendly GET fallbacks: any POST-only step opened directly via browser
# address bar lands the user back on the dashboard instead of a 405/500.
@router.get("/credentials")
@router.get("/start")
@router.get("/code")
@router.get("/password")
@router.get("/disconnect")
@router.get("/join")
async def _telegram_step_get(request: Request) -> Any:
    return RedirectResponse("/auth/telegram", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/credentials")
async def credentials_post(
    request: Request,
    api_id: str = Form(...),
    api_hash: str = Form(...),
) -> Any:
    try:
        session = require_unlocked(request)
    except _NeedsLogin:
        return _redirect_login()

    factory = _session_factory(request)
    if factory is None:
        return await _render_dashboard(
            request,
            session,
            error="База данных недоступна — попробуйте перезагрузить страницу.",
            status_code=503,
        )

    api_id_clean = api_id.strip()
    api_hash_clean = api_hash.strip()
    try:
        api_id_int = int(api_id_clean)
    except ValueError:
        return await _render_dashboard(
            request,
            session,
            error="api_id должен быть целым числом.",
            status_code=400,
        )
    if api_id_int <= 0 or not api_hash_clean:
        return await _render_dashboard(
            request,
            session,
            error="api_id и api_hash не должны быть пустыми.",
            status_code=400,
        )

    await save_api_credentials(factory, bytes(session.dek), api_id_int, api_hash_clean)

    # Sanity-check the round-trip decrypt.
    loaded = await load_api_credentials(factory, bytes(session.dek))
    if loaded is None or loaded != (api_id_int, api_hash_clean):
        log.error("telegram_credentials_roundtrip_mismatch")
        return await _render_dashboard(
            request,
            session,
            error="Не удалось сохранить учётные данные (ошибка шифрования).",
            status_code=500,
        )

    resp = RedirectResponse("/auth/telegram", status_code=status.HTTP_303_SEE_OTHER)
    _set_flash(resp, "ok", "Учётные данные сохранены.")
    return resp


@router.post("/start")
async def start_post(
    request: Request,
    phone: str = Form(...),
) -> Any:
    try:
        session = require_unlocked(request)
    except _NeedsLogin:
        return _redirect_login()

    factory = _session_factory(request)
    if factory is None:
        return await _render_dashboard(
            request,
            session,
            error="База данных недоступна.",
            status_code=503,
        )

    creds = await load_api_credentials(factory, bytes(session.dek))
    if creds is None:
        return await _render_dashboard(
            request,
            session,
            error="Сначала сохраните api_id и api_hash.",
            status_code=400,
        )
    api_id, api_hash = creds

    phone_clean = phone.strip()
    if not phone_clean.startswith("+") or len(phone_clean) < 5:
        return _templates(request).TemplateResponse(
            request,
            "telegram_phone.html",
            {
                "title": "Подключение Telegram",
                "error": "Номер должен быть в формате E.164, например +491701234567.",
            },
            status_code=400,
        )

    # Replace any previous manager on this session.
    pending = _pending_map(request)
    previous = pending.pop(session.token, None)
    if previous is not None:
        with contextlib.suppress(Exception):
            await previous.close()

    # Discover what proxy we'll use BEFORE building the manager so the log
    # tells us if it's None vs MTProto vs SOCKS5.
    pool = getattr(request.app.state, "proxy_pool", None)
    active_proxy = pool.active_url() if pool is not None else None
    log.info(
        "telegram_start_login_begin",
        phone=phone_clean[:5] + "***",
        api_id=api_id,
        active_proxy=active_proxy,
        has_pool=pool is not None,
    )

    manager = _build_manager(session, api_id, api_hash, request=request)
    request.app.state.telegram_manager = manager
    pending[session.token] = manager

    try:
        step = await manager.start_login(phone_clean)
    except TelegramAuthError as exc:
        log.warning("telegram_start_login_auth_error", code=exc.code, error=str(exc))
        return await _render_dashboard(
            request,
            session,
            error=f"Telegram отказал: {exc}",
            status_code=400,
        )
    except Exception as exc:  # noqa: BLE001 — Telethon connection / proxy errors land here
        log.exception("telegram_start_login_unexpected_error", error_type=type(exc).__name__)
        # Drop the half-broken manager so the next attempt builds a fresh one.
        with contextlib.suppress(Exception):
            await manager.close()
        pending.pop(session.token, None)
        if hasattr(request.app.state, "telegram_manager"):
            delattr(request.app.state, "telegram_manager")
        return await _render_dashboard(
            request,
            session,
            error=(
                f"Сетевая ошибка при подключении к Telegram: {exc} "
                "(проверьте /proxy — возможно активный прокси сломан)."
            ),
            status_code=502,
        )

    log.info("telegram_start_login_step", step=step.value)

    # Remember phone so we can show it on the dashboard after success.
    await save_phone(factory, bytes(session.dek), phone_clean)

    if step is AuthStep.CODE:
        return _templates(request).TemplateResponse(
            request,
            "telegram_code.html",
            {"title": "Код подтверждения", "phone": phone_clean, "error": None},
        )

    # Unexpected — fall back to dashboard.
    return await _render_dashboard(request, session)


@router.post("/code")
async def code_post(
    request: Request,
    code: str = Form(...),
) -> Any:
    try:
        session = require_unlocked(request)
    except _NeedsLogin:
        return _redirect_login()

    pending = _pending_map(request)
    manager = pending.get(session.token)
    if manager is None:
        return await _render_dashboard(
            request,
            session,
            error="Нет активной попытки входа — запустите процесс заново.",
            status_code=400,
        )

    code_clean = code.strip().replace(" ", "").replace("-", "")
    try:
        step = await manager.submit_code(code_clean)
    except TelegramAuthError as exc:
        log.info("telegram_submit_code_failed", code=exc.code)
        return _templates(request).TemplateResponse(
            request,
            "telegram_code.html",
            {
                "title": "Код подтверждения",
                "phone": None,
                "error": f"Не принят: {exc}",
            },
            status_code=400,
        )

    if step is AuthStep.PASSWORD:
        return _templates(request).TemplateResponse(
            request,
            "telegram_2fa.html",
            {"title": "Облачный пароль Telegram", "error": None},
        )

    # READY: tidy pending map and flash success back on /chats.
    pending.pop(session.token, None)
    resp = RedirectResponse("/chats", status_code=status.HTTP_303_SEE_OTHER)
    _set_flash(resp, "ok", "Telegram подключён.")
    return resp


@router.post("/password")
async def password_post(
    request: Request,
    password: str = Form(...),
) -> Any:
    try:
        session = require_unlocked(request)
    except _NeedsLogin:
        return _redirect_login()

    pending = _pending_map(request)
    manager = pending.get(session.token)
    if manager is None:
        return await _render_dashboard(
            request,
            session,
            error="Нет активной попытки входа — запустите процесс заново.",
            status_code=400,
        )

    try:
        step = await manager.submit_password(password)
    except TelegramAuthError as exc:
        log.info("telegram_submit_password_failed", code=exc.code)
        return _templates(request).TemplateResponse(
            request,
            "telegram_2fa.html",
            {
                "title": "Облачный пароль Telegram",
                "error": f"Не принят: {exc}",
            },
            status_code=400,
        )

    if step is AuthStep.READY:
        pending.pop(session.token, None)
        resp = RedirectResponse("/chats", status_code=status.HTTP_303_SEE_OTHER)
        _set_flash(resp, "ok", "Telegram подключён.")
        return resp

    return await _render_dashboard(request, session)


@router.post("/disconnect")
async def disconnect_post(request: Request) -> Any:
    try:
        session = require_unlocked(request)
    except _NeedsLogin:
        return _redirect_login()

    mgr: TelegramSessionManager | None = getattr(
        request.app.state, "telegram_manager", None
    )
    if mgr is not None:
        with contextlib.suppress(Exception):
            await mgr.close()
    if hasattr(request.app.state, "telegram_manager"):
        delattr(request.app.state, "telegram_manager")

    pending = _pending_map(request)
    other = pending.pop(session.token, None)
    if other is not None and other is not mgr:
        with contextlib.suppress(Exception):
            await other.close()

    path = _session_file_path()
    if path.exists():
        with contextlib.suppress(OSError):
            path.unlink()

    log.info("telegram_disconnected", token=session.token[:6])
    resp = RedirectResponse("/auth/telegram", status_code=status.HTTP_303_SEE_OTHER)
    _set_flash(resp, "ok", "Telegram отключён.")
    return resp


@router.get("/tdl-login", response_class=HTMLResponse)
async def tdl_login_page(request: Request) -> Any:
    try:
        require_unlocked(request)
    except _NeedsLogin:
        return _redirect_login()
    mgr = _get_or_create_tdl_login(request)
    return _templates(request).TemplateResponse(
        request,
        "tdl_login.html",
        {"title": "tdl session", "status": mgr.status().to_json()},
    )


@router.post("/tdl-login")
async def tdl_login_start(
    request: Request,
    mode: str = Form("qr"),
    passcode: str | None = Form(None),
) -> Any:
    try:
        require_unlocked(request)
    except _NeedsLogin:
        return _redirect_login()
    if mode not in ("qr", "desktop", "code"):
        mode = "qr"
    mgr = _get_or_create_tdl_login(request)
    await mgr.start(mode=mode, desktop_passcode=passcode)
    return RedirectResponse("/auth/telegram/tdl-login", status_code=303)


@router.get("/tdl-login/status")
async def tdl_login_status(request: Request) -> Any:
    try:
        require_unlocked(request)
    except _NeedsLogin:
        return _redirect_login()
    mgr = _get_or_create_tdl_login(request)
    return mgr.status().to_json()


@router.post("/tdl-login/cancel")
async def tdl_login_cancel(request: Request) -> Any:
    try:
        require_unlocked(request)
    except _NeedsLogin:
        return _redirect_login()
    mgr = _get_or_create_tdl_login(request)
    await mgr.cancel()
    return RedirectResponse("/auth/telegram/tdl-login", status_code=303)


def _get_or_create_tdl_login(request: Request):  # noqa: ANN001
    """Lazy singleton on app.state."""
    from src.services.tdl_login import TdlLoginManager

    mgr = getattr(request.app.state, "tdl_login_manager", None)
    if mgr is None:
        s = get_settings()
        storage = s.tdl_storage_dir if getattr(s, "tdl_use_local_storage", False) else None
        if storage is not None:
            storage.mkdir(parents=True, exist_ok=True)
        mgr = TdlLoginManager(
            binary_path=Path(s.tdl_binary_path).resolve(),
            namespace=s.tdl_namespace,
            storage_path=storage,
        )
        request.app.state.tdl_login_manager = mgr
    return mgr


@router.post("/join")
async def join_chat(
    request: Request,
    link: str = Form(...),
) -> Any:
    """Join a private channel / supergroup by invite link.

    Accepts:
      https://t.me/+ABCDEFG…   (modern invite)
      https://t.me/joinchat/ABCDEFG…   (legacy)
      @username                (public; calls JoinChannelRequest)
    """
    try:
        session = require_unlocked(request)
    except _NeedsLogin:
        return _redirect_login()

    mgr: TelegramSessionManager | None = getattr(request.app.state, "telegram_manager", None)
    if mgr is None:
        resp = RedirectResponse("/auth/telegram", status_code=status.HTTP_303_SEE_OTHER)
        _set_flash(resp, "error", "Сначала подключи Telegram.")
        return resp

    raw = (link or "").strip()
    try:
        joined_title = await _join_via_telethon(mgr, raw)
    except TelegramAuthError as exc:
        resp = RedirectResponse("/chats", status_code=status.HTTP_303_SEE_OTHER)
        _set_flash(resp, "error", f"Не удалось присоединиться: {exc}")
        return resp
    except Exception as exc:  # noqa: BLE001
        log.warning("join_chat_failed", error=str(exc), link=raw[:60])
        resp = RedirectResponse("/chats", status_code=status.HTTP_303_SEE_OTHER)
        _set_flash(resp, "error", f"Не удалось присоединиться: {exc}")
        return resp

    log.info("joined_chat", title=joined_title, token=session.token[:6])
    resp = RedirectResponse("/chats", status_code=status.HTTP_303_SEE_OTHER)
    _set_flash(resp, "ok", f"Присоединились: {joined_title}. Нажми «Обновить список».")
    return resp


async def _join_via_telethon(mgr: TelegramSessionManager, link: str) -> str:
    """Returns the chat title the user just joined."""
    client = await mgr._get_client()  # noqa: SLF001 — internal getter is the API surface here
    raw = link.strip()
    # Modern invite: t.me/+HASH or t.me/joinchat/HASH
    invite_hash: str | None = None
    if "joinchat/" in raw:
        invite_hash = raw.rsplit("joinchat/", 1)[-1]
    elif "/+" in raw:
        invite_hash = raw.rsplit("/+", 1)[-1]
    elif raw.startswith("+"):
        invite_hash = raw[1:]

    invite_hash = (invite_hash or "").split("?", 1)[0].split("/", 1)[0].strip()

    if invite_hash:
        from telethon.tl.functions.messages import (  # type: ignore[import-not-found]
            CheckChatInviteRequest,
            ImportChatInviteRequest,
        )

        # First check — surfaces "already a participant" cleanly.
        try:
            check = await client(CheckChatInviteRequest(invite_hash))
            if hasattr(check, "chat") and check.chat is not None:
                return getattr(check.chat, "title", invite_hash)
        except Exception:  # noqa: BLE001 — fall through to import
            pass
        result = await client(ImportChatInviteRequest(invite_hash))
        chats = getattr(result, "chats", []) or []
        if chats:
            return getattr(chats[0], "title", invite_hash)
        return invite_hash

    # Public username: @name or https://t.me/name
    username = raw
    if username.startswith("https://t.me/") or username.startswith("http://t.me/"):
        username = username.rsplit("/", 1)[-1]
    if username.startswith("@"):
        username = username[1:]
    if not username:
        raise TelegramAuthError("link_invalid", f"Не распознал ссылку: {link!r}")

    from telethon.tl.functions.channels import (  # type: ignore[import-not-found]
        JoinChannelRequest,
    )

    entity = await client.get_entity(username)
    await client(JoinChannelRequest(entity))
    return getattr(entity, "title", None) or username


# Expose dependency for test injection / external reuse.
__all__ = ["router", "require_unlocked"]

# Silence `Depends` unused-import warning while still exporting it for users.
_ = Depends
