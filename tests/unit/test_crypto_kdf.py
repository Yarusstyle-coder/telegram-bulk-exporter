"""Argon2id KDF tests — determinism, salt sensitivity, parameter plumbing."""

from __future__ import annotations

import pytest

from src.crypto.kdf import HASH_LEN, derive_kek, new_salt

FAST = {"memory_cost": 8192, "time_cost": 1, "parallelism": 1}


def test_derive_kek_is_deterministic() -> None:
    salt = b"\x00" * 16
    a = derive_kek("hunter2", salt, **FAST)
    b = derive_kek("hunter2", salt, **FAST)
    assert a == b
    assert len(a) == HASH_LEN


def test_derive_kek_different_salt_different_output() -> None:
    a = derive_kek("hunter2", b"\x00" * 16, **FAST)
    b = derive_kek("hunter2", b"\x01" * 16, **FAST)
    assert a != b


def test_derive_kek_different_password_different_output() -> None:
    salt = b"\x00" * 16
    a = derive_kek("hunter2", salt, **FAST)
    b = derive_kek("hunter3", salt, **FAST)
    assert a != b


def test_derive_kek_memory_cost_changes_output() -> None:
    salt = b"\x00" * 16
    a = derive_kek("hunter2", salt, memory_cost=8192, time_cost=1, parallelism=1)
    b = derive_kek("hunter2", salt, memory_cost=16384, time_cost=1, parallelism=1)
    assert a != b


def test_derive_kek_time_cost_changes_output() -> None:
    salt = b"\x00" * 16
    a = derive_kek("hunter2", salt, memory_cost=8192, time_cost=1, parallelism=1)
    b = derive_kek("hunter2", salt, memory_cost=8192, time_cost=2, parallelism=1)
    assert a != b


def test_new_salt_length_and_randomness() -> None:
    a = new_salt()
    b = new_salt()
    assert len(a) == 16
    assert len(b) == 16
    assert a != b


def test_derive_kek_rejects_bytes_password() -> None:
    with pytest.raises(TypeError):
        derive_kek(b"bytes-not-str", b"\x00" * 16, **FAST)  # type: ignore[arg-type]


def test_derive_kek_rejects_short_salt() -> None:
    with pytest.raises(ValueError):
        derive_kek("hunter2", b"\x00", **FAST)
