"""Sliding-window rate limiter tests."""

from __future__ import annotations

import time

from src.auth.rate_limit import RateLimiter


def test_five_attempts_pass_sixth_blocked() -> None:
    rl = RateLimiter(max_attempts=5, window_seconds=60.0, penalty_seconds=60.0)
    for _ in range(5):
        assert rl.attempt("user1") is True
    assert rl.attempt("user1") is False


def test_different_keys_are_independent() -> None:
    rl = RateLimiter(max_attempts=2, window_seconds=60.0, penalty_seconds=60.0)
    assert rl.attempt("a") is True
    assert rl.attempt("a") is True
    assert rl.attempt("a") is False
    assert rl.attempt("b") is True
    assert rl.attempt("b") is True


def test_reset_clears_state() -> None:
    rl = RateLimiter(max_attempts=2, window_seconds=60.0, penalty_seconds=60.0)
    rl.attempt("u")
    rl.attempt("u")
    assert rl.attempt("u") is False
    rl.reset("u")
    assert rl.attempt("u") is True


def test_window_expiry_allows_new_attempts() -> None:
    rl = RateLimiter(max_attempts=3, window_seconds=0.05, penalty_seconds=0.0)
    for _ in range(3):
        assert rl.attempt("u") is True
    # Immediately blocked.
    assert rl.attempt("u") is False
    # After window + penalty clears, we can attempt again.
    time.sleep(0.1)
    assert rl.attempt("u") is True


def test_lockout_outlasts_window() -> None:
    rl = RateLimiter(max_attempts=2, window_seconds=0.05, penalty_seconds=0.2)
    rl.attempt("u")
    rl.attempt("u")
    rl.attempt("u")  # triggers lockout
    assert rl.is_locked("u") is True
    time.sleep(0.08)  # past window but still locked
    assert rl.attempt("u") is False
    time.sleep(0.25)
    assert rl.attempt("u") is True
