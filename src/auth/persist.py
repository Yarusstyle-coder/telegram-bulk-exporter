"""Persist the session token + DEK across server restarts via OS secret store.

Windows DPAPI / macOS Keychain / libsecret. The blob is OS-user-bound;
copying the file to another machine doesn't yield the secret.

We persist the *cookie token* alongside the DEK so the client's old cookie
still matches after a restart and the user simply keeps browsing.

Public API:

    save(token, dek)  — store/refresh
    load() -> (token, dek) | None
    forget()
"""

from __future__ import annotations

import base64
import json

from src.logging_setup import get_logger

log = get_logger(__name__)

_SERVICE = "telegram-bulk-exporter"
_USER = "session-v1"


def _backend():
    try:
        import keyring  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        log.warning("keyring_import_failed", error=str(exc))
        return None
    return keyring


def save(token: str, dek: bytes) -> bool:
    kr = _backend()
    if kr is None:
        return False
    payload = json.dumps({"token": token, "dek": base64.b64encode(dek).decode("ascii")})
    try:
        kr.set_password(_SERVICE, _USER, payload)
        log.info("session_persisted", token=token[:6])
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("session_persist_failed", error=str(exc))
        return False


def load() -> tuple[str, bytes] | None:
    kr = _backend()
    if kr is None:
        return None
    try:
        raw = kr.get_password(_SERVICE, _USER)
    except Exception as exc:  # noqa: BLE001
        log.warning("session_load_failed", error=str(exc))
        return None
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        token = obj["token"]
        dek = base64.b64decode(obj["dek"])
        return token, dek
    except Exception as exc:  # noqa: BLE001
        log.warning("session_decode_failed", error=str(exc))
        return None


def forget() -> bool:
    kr = _backend()
    if kr is None:
        return False
    try:
        kr.delete_password(_SERVICE, _USER)
        log.info("session_forgotten")
        return True
    except Exception as exc:  # noqa: BLE001 — likely PasswordDeleteError on miss
        log.info("session_forget_no_op", error=str(exc))
        return False


# Back-compat aliases (older callers referenced these names).
save_dek = lambda dek: save("", dek)  # noqa: E731
load_dek = lambda: (load() or (None, None))[1]  # noqa: E731
forget_dek = forget
