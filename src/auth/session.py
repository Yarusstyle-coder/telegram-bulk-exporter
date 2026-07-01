"""In-memory session store that holds the DEK for the life of a session.

The DEK is stored as a `bytearray` so it can be zeroized on lock / expiry via
`secure_zero`.  Auto-lock is enforced by `get()` and `purge_expired()` — both
treat expired sessions as absent and zero out the key.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

from src.crypto.memory import secure_zero

_TOKEN_BYTES = 32  # 256-bit session token


@dataclass
class Session:
    token: str
    created_at: float
    last_seen: float
    dek: bytearray  # zeroed on lock / expiry
    username: str | None = None
    two_fa_passed: bool = False
    setup_stage: str | None = None  # "password" → "totp" → "backup" → None (done)
    pending_totp_secret: str | None = None  # unconfirmed secret during setup
    pending_backup_codes: list[str] = field(default_factory=list)

    def is_locked(self) -> bool:
        return len(self.dek) == 0 or all(b == 0 for b in self.dek)


class SessionStore:
    """Thread-safe mapping of token → `Session`."""

    def __init__(self, auto_lock_seconds: float = 900.0) -> None:
        self.auto_lock_seconds = auto_lock_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, dek: bytes, *, username: str | None = None) -> Session:
        """Create a new session carrying a fresh copy of `dek`.

        The caller may overwrite their original `dek` buffer afterwards.
        """
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        return self.restore(token=token, dek=dek, username=username)

    def restore(
        self,
        *,
        token: str,
        dek: bytes,
        username: str | None = None,
        two_fa_passed: bool = False,
    ) -> Session:
        """Recreate a session with a known token (used after server restart
        when the token was persisted in the OS secret store)."""
        now = time.monotonic()
        session = Session(
            token=token,
            created_at=now,
            last_seen=now,
            dek=bytearray(dek),
            username=username,
            two_fa_passed=two_fa_passed,
        )
        with self._lock:
            self._sessions[token] = session
        return session

    def _is_expired(self, session: Session, now: float) -> bool:
        # auto_lock_seconds <= 0 means "never auto-lock" — used when the
        # user opts into a permanent local session (single-user dev box).
        if self.auto_lock_seconds <= 0:
            return False
        return session.last_seen + self.auto_lock_seconds < now

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        now = time.monotonic()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if self._is_expired(session, now):
                secure_zero(session.dek)
                self._sessions.pop(token, None)
                return None
            return session

    def touch(self, token: str) -> None:
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return
            session.last_seen = time.monotonic()

    def lock(self, token: str) -> None:
        """Zero the DEK and remove the session from the store."""
        with self._lock:
            session = self._sessions.pop(token, None)
        if session is not None:
            secure_zero(session.dek)

    def purge_expired(self) -> int:
        """Zero + remove all expired sessions. Returns the number removed."""
        now = time.monotonic()
        removed = 0
        with self._lock:
            dead = [t for t, s in self._sessions.items() if self._is_expired(s, now)]
            for t in dead:
                session = self._sessions.pop(t)
                secure_zero(session.dek)
                removed += 1
        return removed

    def all_sessions(self) -> list[Session]:  # pragma: no cover - debug aid
        with self._lock:
            return list(self._sessions.values())
