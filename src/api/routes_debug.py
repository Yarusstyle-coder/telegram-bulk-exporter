"""Diagnostic routes — useful when reproducing 'why am I logged out'.

Mounted unconditionally; protected by the same session middleware as
everything else, so anonymous callers get redirected to /login.

GET /debug/session   → JSON snapshot of cookie + SessionStore + keyring.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from src.config import get_settings

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/session")
async def session_debug(request: Request) -> dict[str, Any]:
    settings = get_settings()
    cookie = request.cookies.get(settings.session_cookie_name)
    cookie_short = f"{cookie[:8]}…" if cookie else None

    # SessionStore state
    sess_info: dict[str, Any] = {"present": False}
    try:
        from src.api.routes_auth import SESSION_STORE

        s = SESSION_STORE.get(cookie) if cookie else None
        if s is not None:
            sess_info = {
                "present": True,
                "two_fa_passed": s.two_fa_passed,
                "username": s.username,
                "auto_lock_seconds": SESSION_STORE.auto_lock_seconds,
            }
    except Exception as exc:  # noqa: BLE001
        sess_info["error"] = str(exc)

    # Keyring state
    keyring_info: dict[str, Any] = {"persisted": False}
    try:
        from src.auth.persist import load

        kr = load()
        if kr is not None:
            tok, dek = kr
            keyring_info = {
                "persisted": True,
                "token_short": f"{tok[:8]}…",
                "matches_cookie": (cookie == tok),
                "dek_bytes": len(dek),
            }
    except Exception as exc:  # noqa: BLE001
        keyring_info["error"] = str(exc)

    # Telegram manager
    mgr = getattr(request.app.state, "telegram_manager", None)
    tg_info: dict[str, Any] = {"present": mgr is not None}
    if mgr is not None:
        try:
            tg_info["authorized"] = await mgr.is_authorized()
        except Exception as exc:  # noqa: BLE001
            tg_info["error"] = str(exc)

    return {
        "cookie_name": settings.session_cookie_name,
        "cookie_short": cookie_short,
        "session": sess_info,
        "keyring": keyring_info,
        "telegram": tg_info,
        "persist_sessions": settings.persist_sessions,
        "auto_lock_seconds": settings.auto_lock_seconds,
    }
