"""Session store tests — create, get, touch, lock, auto-lock, zeroization."""

from __future__ import annotations

import time

from src.auth.session import SessionStore


def test_create_and_get() -> None:
    store = SessionStore(auto_lock_seconds=60.0)
    s = store.create(b"\x11" * 32, username="admin")
    got = store.get(s.token)
    assert got is s
    assert got.username == "admin"
    assert bytes(got.dek) == b"\x11" * 32


def test_get_returns_none_for_unknown_token() -> None:
    store = SessionStore(auto_lock_seconds=60.0)
    assert store.get("nope") is None
    assert store.get(None) is None


def test_touch_refreshes_last_seen() -> None:
    store = SessionStore(auto_lock_seconds=60.0)
    s = store.create(b"\x00" * 32)
    initial = s.last_seen
    time.sleep(0.05)
    store.touch(s.token)
    assert s.last_seen > initial


def test_lock_zeros_dek_and_removes_session() -> None:
    store = SessionStore(auto_lock_seconds=60.0)
    s = store.create(b"\x22" * 32)
    token = s.token
    store.lock(token)
    assert store.get(token) is None
    # The original session object's dek is zeroed out.
    assert bytes(s.dek) == b"\x00" * 32


def test_auto_lock_expires_session() -> None:
    store = SessionStore(auto_lock_seconds=0.05)
    s = store.create(b"\x33" * 32)
    time.sleep(0.1)
    assert store.get(s.token) is None
    # After expiry, the DEK is zeroed.
    assert bytes(s.dek) == b"\x00" * 32


def test_purge_expired_reports_count() -> None:
    store = SessionStore(auto_lock_seconds=0.05)
    a = store.create(b"\x01" * 32)
    b = store.create(b"\x02" * 32)
    time.sleep(0.1)
    # Create a third one AFTER the first two are expired.
    c = store.create(b"\x03" * 32)

    removed = store.purge_expired()
    assert removed == 2
    assert store.get(a.token) is None
    assert store.get(b.token) is None
    assert store.get(c.token) is not None


def test_dek_buffer_is_independent_copy() -> None:
    """Modifying the caller's bytes after create shouldn't affect the session."""
    store = SessionStore(auto_lock_seconds=60.0)
    original = bytearray(b"\xAA" * 32)
    s = store.create(bytes(original))
    # Overwrite caller's buffer.
    for i in range(len(original)):
        original[i] = 0
    assert bytes(s.dek) == b"\xAA" * 32
