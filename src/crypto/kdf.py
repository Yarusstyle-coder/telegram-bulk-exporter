"""Argon2id KDF wrapper.

Parameters were tuned for ~500ms on a typical laptop.  Tests override them to
keep the suite fast via `derive_kek(..., memory_cost=8192, time_cost=1)`.
"""

from __future__ import annotations

import secrets

from argon2.low_level import Type, hash_secret_raw

MEMORY_COST = 47104  # KiB (~46 MiB)
TIME_COST = 1
PARALLELISM = 1
HASH_LEN = 32
SALT_LEN = 16


def derive_kek(
    password: str,
    salt: bytes,
    *,
    memory_cost: int = MEMORY_COST,
    time_cost: int = TIME_COST,
    parallelism: int = PARALLELISM,
) -> bytes:
    """Derive a 32-byte Key-Encryption-Key from a user password using Argon2id.

    Args:
        password: Master password (unicode, arbitrary length).
        salt: Random salt (>=16 bytes recommended).
        memory_cost: KiB of memory, default ~46 MiB.
        time_cost: Iterations.
        parallelism: Lanes.
    """
    if not isinstance(password, str):
        raise TypeError("password must be str")
    if len(salt) < 8:
        raise ValueError("salt too short")

    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        hash_len=HASH_LEN,
        type=Type.ID,
    )


def new_salt() -> bytes:
    """Return a cryptographically-random salt of `SALT_LEN` bytes."""
    return secrets.token_bytes(SALT_LEN)
