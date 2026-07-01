"""FastAPI routes for setup / login / lock / change-password flows.

State that survives across requests lives in two module-level singletons:

- `SESSION_STORE`: DEK-bearing session registry (auto-lock enforced).
- `LOGIN_LIMITER`: per-IP rate limiter for `/login` + `/login/2fa`.

Cookies are httpOnly + SameSite=Strict; `Secure` is off because the app is
localhost-only by design.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from zxcvbn import zxcvbn

from src.auth.backup_codes import (
    generate_backup_codes,
    hash_code,
    verify_code,
)
from src.auth.rate_limit import RateLimiter
from src.auth.session import Session, SessionStore
from src.auth.store import AuthData, AuthStore
from src.auth.totp import new_secret, provisioning_uri, qr_png, verify
from src.config import get_settings
from src.crypto.vault import (
    InvalidPassword,
    create_vault,
    unlock_vault,
    vault_exists,
)
from src.crypto.vault import (
    change_password as vault_change_password,
)
from src.logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["auth"])

# Module-level singletons — reset by `reset_auth_state()` in tests.
SESSION_STORE = SessionStore(auto_lock_seconds=get_settings().auto_lock_seconds)
LOGIN_LIMITER = RateLimiter(max_attempts=5, window_seconds=300.0, penalty_seconds=300.0)


def reset_auth_state() -> None:
    """Re-initialize module-level singletons (test helper)."""
    global SESSION_STORE, LOGIN_LIMITER
    SESSION_STORE = SessionStore(auto_lock_seconds=get_settings().auto_lock_seconds)
    LOGIN_LIMITER = RateLimiter(max_attempts=5, window_seconds=300.0, penalty_seconds=300.0)


# ---------------------------------------------------------------------------
# Dependencies / helpers
# ---------------------------------------------------------------------------


def _vault_path() -> Path:
    return get_settings().vault_path


def _auth_store_path() -> Path:
    return get_settings().data_dir / "auth.json"


def _templates(request: Request):
    return request.app.state.templates


def _cookie_name() -> str:
    return get_settings().session_cookie_name


def _client_ip(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


def _get_session(request: Request) -> Session | None:
    token = request.cookies.get(_cookie_name())
    return SESSION_STORE.get(token)


def _require_unlocked(request: Request) -> Session:
    session = _get_session(request)
    if session is None or not session.two_fa_passed:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    SESSION_STORE.touch(session.token)
    return session


def _set_session_cookie(resp: Response, token: str) -> None:
    # Source of truth is the live SESSION_STORE — it gets updated when
    # the user changes the auto-lock window via /login/2fa. Fall back
    # to the static settings value when the store hasn't been touched
    # yet (cold start, before the first successful 2FA).
    auto_lock = getattr(SESSION_STORE, "auto_lock_seconds", None)
    if not isinstance(auto_lock, (int, float)) or auto_lock < 0:
        auto_lock = get_settings().auto_lock_seconds
    # 0 means 'never auto-lock' — emit a long-lived cookie (10 years).
    max_age = int(auto_lock) if auto_lock > 0 else 60 * 60 * 24 * 365 * 10
    resp.set_cookie(
        _cookie_name(),
        token,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        secure=False,  # localhost only
        path="/",
    )


def _clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(_cookie_name(), path="/")


def _auth_store() -> AuthStore:
    return AuthStore(_auth_store_path())


# ---------------------------------------------------------------------------
# Setup flow
# ---------------------------------------------------------------------------


@router.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request) -> Any:
    if vault_exists(_vault_path()):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return _templates(request).TemplateResponse(
        request,
        "setup.html",
        {"title": "Первоначальная настройка", "error": None},
    )


@router.post("/setup", response_class=HTMLResponse)
async def setup_post(
    request: Request,
    password: str = Form(...),
    password_confirm: str = Form(...),
) -> Any:
    if vault_exists(_vault_path()):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    if password != password_confirm:
        return _templates(request).TemplateResponse(
            request,
            "setup.html",
            {"title": "Первоначальная настройка", "error": "Пароли не совпадают."},
            status_code=400,
        )

    score_result = zxcvbn(password)
    if score_result["score"] < 3:
        feedback = score_result.get("feedback", {})
        warn = feedback.get("warning") or "Пароль слишком слабый."
        suggestions = feedback.get("suggestions") or []
        msg = warn + (" " + " ".join(suggestions) if suggestions else "")
        return _templates(request).TemplateResponse(
            request,
            "setup.html",
            {"title": "Первоначальная настройка", "error": msg},
            status_code=400,
        )

    get_settings().ensure_dirs()
    dek = create_vault(password, _vault_path())

    # Initialize empty auth store.
    store = _auth_store()
    store.save(dek, AuthData())

    session = SESSION_STORE.create(dek, username="admin")
    session.setup_stage = "totp"

    resp = RedirectResponse("/setup/totp", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(resp, session.token)
    return resp


@router.get("/setup/totp", response_class=HTMLResponse)
async def setup_totp_get(request: Request) -> Any:
    session = _get_session(request)
    if session is None or session.setup_stage not in {"totp", "backup"}:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    SESSION_STORE.touch(session.token)

    if session.pending_totp_secret is None:
        session.pending_totp_secret = new_secret()

    uri = provisioning_uri(session.pending_totp_secret, account="admin")
    png = qr_png(uri)
    qr_data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    return _templates(request).TemplateResponse(
        request,
        "setup_totp.html",
        {
            "title": "Настройка TOTP",
            "secret": session.pending_totp_secret,
            "qr_data_uri": qr_data_uri,
            "error": None,
        },
    )


@router.post("/setup/totp/verify", response_class=HTMLResponse)
async def setup_totp_verify(
    request: Request,
    code: str = Form(...),
) -> Any:
    session = _get_session(request)
    if session is None or session.setup_stage != "totp":
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    SESSION_STORE.touch(session.token)

    secret = session.pending_totp_secret
    if not secret or not verify(secret, code):
        # Re-render page with error.
        uri = provisioning_uri(secret or "", account="admin")
        png = qr_png(uri) if secret else b""
        qr_data_uri = (
            ("data:image/png;base64," + base64.b64encode(png).decode("ascii"))
            if png
            else ""
        )
        return _templates(request).TemplateResponse(
            request,
            "setup_totp.html",
            {
                "title": "Настройка TOTP",
                "secret": secret,
                "qr_data_uri": qr_data_uri,
                "error": "Код не подходит. Проверьте время устройства и попробуйте ещё раз.",
            },
            status_code=400,
        )

    # Persist secret. We don't mark setup_complete until backup codes are confirmed.
    store = _auth_store()
    data = store.load(bytes(session.dek))
    data.totp_secret = secret
    data.totp_enabled = True
    store.save(bytes(session.dek), data)

    session.setup_stage = "backup"
    return RedirectResponse(
        "/setup/backup-codes", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/setup/backup-codes", response_class=HTMLResponse)
async def setup_backup_codes_get(request: Request) -> Any:
    session = _get_session(request)
    if session is None or session.setup_stage != "backup":
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    SESSION_STORE.touch(session.token)

    if not session.pending_backup_codes:
        codes = generate_backup_codes(10)
        session.pending_backup_codes = codes
        hashes = [hash_code(c) for c in codes]

        store = _auth_store()
        data = store.load(bytes(session.dek))
        data.backup_code_hashes = hashes
        data.backup_codes_used = [False] * len(hashes)
        store.save(bytes(session.dek), data)

    return _templates(request).TemplateResponse(
        request,
        "setup_backup_codes.html",
        {
            "title": "Резервные коды",
            "codes": session.pending_backup_codes,
        },
    )


@router.post("/setup/backup-codes/confirm", response_class=HTMLResponse)
async def setup_backup_codes_confirm(
    request: Request,
    saved: str = Form(...),
) -> Any:
    session = _get_session(request)
    if session is None or session.setup_stage != "backup":
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if saved.lower() not in {"on", "true", "1", "yes"}:
        return RedirectResponse(
            "/setup/backup-codes", status_code=status.HTTP_303_SEE_OTHER
        )

    store = _auth_store()
    data = store.load(bytes(session.dek))
    data.setup_complete = True
    store.save(bytes(session.dek), data)

    session.setup_stage = None
    session.two_fa_passed = True
    session.pending_totp_secret = None
    session.pending_backup_codes = []

    _maybe_persist_session(session)

    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request) -> Any:
    if not vault_exists(_vault_path()):
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
    return _templates(request).TemplateResponse(
        request,
        "login.html",
        {"title": "Вход", "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    password: str = Form(...),
) -> Any:
    if not vault_exists(_vault_path()):
        return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)

    ip = _client_ip(request)
    if not LOGIN_LIMITER.attempt(f"login:{ip}"):
        return _templates(request).TemplateResponse(
            request,
            "login.html",
            {
                "title": "Вход",
                "error": "Слишком много попыток. Попробуйте позже.",
            },
            status_code=429,
        )

    try:
        dek = unlock_vault(password, _vault_path())
    except InvalidPassword:
        return _templates(request).TemplateResponse(
            request,
            "login.html",
            {"title": "Вход", "error": "Неверный пароль."},
            status_code=400,
        )

    LOGIN_LIMITER.reset(f"login:{ip}")

    session = SESSION_STORE.create(dek, username="admin")

    # Check if 2FA is required.
    store = _auth_store()
    try:
        data = store.load(dek)
    except Exception:
        data = AuthData()

    if not data.totp_enabled or not data.setup_complete:
        # Setup never finished — push back into setup flow.
        session.setup_stage = "totp" if not data.totp_enabled else "backup"
        target = "/setup/totp" if not data.totp_enabled else "/setup/backup-codes"
        resp = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
        _set_session_cookie(resp, session.token)
        return resp

    resp = RedirectResponse("/login/2fa", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(resp, session.token)
    return resp


@router.get("/login/2fa", response_class=HTMLResponse)
async def login_2fa_get(request: Request) -> Any:
    session = _get_session(request)
    if session is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if session.two_fa_passed:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    SESSION_STORE.touch(session.token)

    from src.auth.user_prefs import LOCK_DURATION_CHOICES, load_user_prefs, seconds_to_slug

    prefs = load_user_prefs()
    return _templates(request).TemplateResponse(
        request,
        "login_2fa.html",
        {
            "title": "Двухфакторная проверка",
            "error": None,
            "lock_choices": LOCK_DURATION_CHOICES,
            "current_lock_slug": seconds_to_slug(prefs.get("auto_lock_seconds", 0)),
        },
    )


@router.post("/login/2fa", response_class=HTMLResponse)
async def login_2fa_post(
    request: Request,
    code: str = Form(...),
    remember: str | None = Form(None),
    lock_duration: str = Form("until_restart"),
) -> Any:
    from src.auth.user_prefs import (
        LOCK_DURATION_CHOICES,
        load_user_prefs,
        save_user_prefs,
        seconds_to_slug,
        slug_to_seconds,
    )

    def _template_ctx(error: str | None) -> dict[str, Any]:
        prefs = load_user_prefs()
        return {
            "title": "Двухфакторная проверка",
            "error": error,
            "lock_choices": LOCK_DURATION_CHOICES,
            "current_lock_slug": seconds_to_slug(prefs.get("auto_lock_seconds", 0)),
        }

    session = _get_session(request)
    if session is None:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    ip = _client_ip(request)
    if not LOGIN_LIMITER.attempt(f"2fa:{ip}"):
        return _templates(request).TemplateResponse(
            request,
            "login_2fa.html",
            _template_ctx("Слишком много попыток. Попробуйте позже."),
            status_code=429,
        )

    store = _auth_store()
    data = store.load(bytes(session.dek))

    code_norm = code.strip()
    accepted = False

    # First try TOTP (6–8 digits).
    if (
        data.totp_secret
        and code_norm.replace(" ", "").replace("-", "").isdigit()
        and verify(data.totp_secret, code_norm)
    ):
        accepted = True

    # Fallback to backup code.
    if not accepted and data.backup_code_hashes:
        unused_hashes = [
            h if not used else ""
            for h, used in zip(
                data.backup_code_hashes, data.backup_codes_used, strict=False
            )
        ]
        idx = verify_code(code_norm, unused_hashes)
        if idx is not None:
            data.backup_codes_used[idx] = True
            store.save(bytes(session.dek), data)
            accepted = True

    if not accepted:
        return _templates(request).TemplateResponse(
            request,
            "login_2fa.html",
            _template_ctx("Неверный код."),
            status_code=400,
        )

    LOGIN_LIMITER.reset(f"2fa:{ip}")
    session.two_fa_passed = True
    SESSION_STORE.touch(session.token)

    # Persist the chosen auto-lock window so the next /login/2fa GET
    # shows the right radio pre-checked AND the next server start
    # restores SESSION_STORE.auto_lock_seconds without prompting.
    chosen_seconds = slug_to_seconds(lock_duration)
    if chosen_seconds is None:
        # Unknown slug — keep the current preference instead of silently
        # forcing "never lock". Should never happen unless someone POSTs
        # a hand-crafted form.
        chosen_seconds = load_user_prefs().get("auto_lock_seconds", 0)
    save_user_prefs({"auto_lock_seconds": int(chosen_seconds)})
    SESSION_STORE.auto_lock_seconds = float(chosen_seconds)
    log.info(
        "auto_lock_updated",
        auto_lock_seconds=chosen_seconds,
        slug=lock_duration,
    )
    # Persist to OS keyring only if user opted in (default: yes — checked).
    if remember:
        _maybe_persist_session(session)
    else:
        # User unchecked the box — clear any previously persisted session
        # so a restart actually drops them.
        try:
            from src.auth.persist import forget

            forget()
        except Exception:  # noqa: BLE001
            pass
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


def _maybe_persist_session(session: Session) -> None:
    """If `settings.persist_sessions` is on, persist (token, DEK) to the OS
    secret store. The next server start restores them (see `lifespan`)."""
    s = get_settings()
    if not s.persist_sessions:
        return
    try:
        from src.auth.persist import save

        save(session.token, bytes(session.dek))
    except Exception as exc:  # noqa: BLE001
        log.warning("session_persist_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Lock / change-password
# ---------------------------------------------------------------------------


@router.post("/lock")
async def lock_post(request: Request) -> Response:
    token = request.cookies.get(_cookie_name())
    if token:
        SESSION_STORE.lock(token)
    # Lock means "drop the DEK from RAM AND from persistent storage" so a
    # restart re-prompts for the password.
    try:
        from src.auth.persist import forget_dek

        forget_dek()
    except Exception as exc:  # noqa: BLE001
        log.warning("session_forget_failed", error=str(exc))
    resp = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _clear_session_cookie(resp)
    return resp


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_post(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
) -> Any:
    session = _require_unlocked(request)

    if new_password != new_password_confirm:
        raise HTTPException(status_code=400, detail="новые пароли не совпадают")
    if zxcvbn(new_password)["score"] < 3:
        raise HTTPException(status_code=400, detail="новый пароль слишком слабый")

    try:
        vault_change_password(old_password, new_password, _vault_path())
    except InvalidPassword as exc:
        raise HTTPException(status_code=400, detail="старый пароль неверен") from exc

    # Session DEK stays valid because change_password doesn't rotate the DEK.
    log.info("password_changed", session=session.token[:6])
    return Response(status_code=204)
