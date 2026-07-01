"""Serve cached Telegram avatar PNGs.

Endpoint ``GET /avatars/{chat_id}`` returns the JPEG written by
``TelegramSessionManager.download_avatar`` (``settings.avatars_dir``).  If the
file is missing, we return a 1x1 transparent PNG so the UI doesn't need to
handle 404s separately; the ``<img>`` fallback inside `_chats_fragment.html`
uses initials when there's no avatar_path at all, so the transparent fallback
only shows up during a tiny refresh race.

All responses are gated behind the standard logged-in-session middleware in
:mod:`src.main`; we additionally assert the session here so direct WS upgrades
or unusual middlewares can't bypass it.

A long-lived private cache is set so repeated navigation doesn't hit disk.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from src.config import get_settings
from src.logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/avatars", tags=["avatars"])


# 1×1 transparent PNG (67 bytes). Inlined so we don't need a static asset.
_TRANSPARENT_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c62000100000005000100b5f3190b0000000049454e"
    "44ae426082"
)

_CACHE_HEADERS: dict[str, str] = {"Cache-Control": "private, max-age=3600"}


def _require_logged_in(request: Request) -> bool:
    """Return True when the session cookie resolves to a 2FA-passed session."""
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return False
    try:
        from src.api.routes_auth import SESSION_STORE
    except Exception:  # pragma: no cover - auth router not mounted
        return False
    session = SESSION_STORE.get(token)
    return session is not None and session.two_fa_passed


@router.get("/{chat_id}")
async def get_avatar(chat_id: int, request: Request) -> Response:
    if not _require_logged_in(request):
        return RedirectResponse("/login", status_code=303)

    settings = get_settings()
    path = settings.avatars_dir / f"{chat_id}.jpg"
    if path.exists() and path.is_file():
        return FileResponse(
            str(path),
            media_type="image/jpeg",
            headers=_CACHE_HEADERS,
        )

    return Response(
        content=_TRANSPARENT_PNG,
        media_type="image/png",
        headers=_CACHE_HEADERS,
    )
