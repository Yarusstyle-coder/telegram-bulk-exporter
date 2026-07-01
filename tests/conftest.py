"""Pytest fixtures shared across the suite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.config import Settings, reset_settings_cache


@pytest.fixture
def tmp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Override Settings paths to a tmp_path sandbox for the duration of a test."""
    data = tmp_path / "data"
    exports = tmp_path / "exports"
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("EXPORT_DIR", str(exports))
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "deadbeefdeadbeefdeadbeefdeadbeef")
    reset_settings_cache()
    settings = Settings()
    settings.ensure_dirs()
    yield settings
    reset_settings_cache()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from seeing a developer's actual .env values."""
    for key in list(os.environ):
        if key.startswith("TELEGRAM_"):
            monkeypatch.delenv(key, raising=False)
    # Proxy isolation + speed. A developer's real ``PROXY`` / ``PROXIES``
    # env vars otherwise seed the pool, and the default
    # ``proxy_auto_select=True`` makes EVERY app-building test probe those
    # proxies over the network at lifespan startup (~4 s) and tear the
    # retest task down on shutdown (~7 s) — that alone turned the suite
    # into a ~9-minute crawl and let tests hit the dev's real proxies.
    # Tests must never touch the network: drop the seed + disable
    # auto-select. Proxy-specific tests drive ``ProxyPool`` directly and
    # are unaffected.
    for key in ("PROXY", "PROXIES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PROXY_AUTO_SELECT", "false")
    reset_settings_cache()
