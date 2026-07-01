"""One-time backup codes — Crockford base32, Argon2id-hashed at rest."""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# Crockford base32 alphabet: no I, L, O, U to avoid confusion.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Tuned low enough not to slow the login path significantly.
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32)


def _normalize(code: str) -> str:
    """Uppercase, strip spaces + dashes. Apply Crockford I/L→1, O→0 aliases."""
    s = code.strip().upper().replace("-", "").replace(" ", "")
    s = s.replace("I", "1").replace("L", "1").replace("O", "0")
    return s


def _random_code() -> str:
    raw = [secrets.choice(_ALPHABET) for _ in range(12)]
    return f"{''.join(raw[0:4])}-{''.join(raw[4:8])}-{''.join(raw[8:12])}"


def generate_backup_codes(count: int = 10) -> list[str]:
    """Return `count` unique human-formatted backup codes."""
    if count <= 0:
        raise ValueError("count must be positive")
    seen: set[str] = set()
    codes: list[str] = []
    while len(codes) < count:
        c = _random_code()
        norm = _normalize(c)
        if norm in seen:
            continue
        seen.add(norm)
        codes.append(c)
    return codes


def hash_code(code: str) -> str:
    """Argon2id hash of the normalized code."""
    return _hasher.hash(_normalize(code))


def verify_code(code: str, hashes: list[str]) -> int | None:
    """Return the index of the `hashes` entry that matches `code`, or None.

    The caller is responsible for marking the matched hash consumed.
    """
    norm = _normalize(code)
    if not norm:
        return None
    for i, h in enumerate(hashes):
        if not h:
            continue
        try:
            if _hasher.verify(h, norm):
                return i
        except VerifyMismatchError:
            continue
        except Exception:  # pragma: no cover - defensive
            continue
    return None
