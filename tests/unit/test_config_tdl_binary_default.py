"""Platform-dependent default for ``Settings.tdl_binary_path``.

tdl ships as ``tdl.exe`` on Windows and a bare ``tdl`` on Linux/macOS —
the packaged binary in ``tools/tdl/`` never has a ``.exe`` suffix outside
Windows. The default must reflect ``sys.platform`` so a fresh checkout
on Linux/macOS doesn't point at a nonexistent ``tdl.exe``. An explicit
``TDL_BINARY_PATH`` env var (or ``.env`` entry) must still win over the
platform-derived default — that's normal pydantic-settings precedence,
unaffected by how the default itself is computed.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make sure a stray TDL_BINARY_PATH from the outer shell / .env
    # doesn't leak into these tests and mask the default we're checking.
    monkeypatch.delenv("TDL_BINARY_PATH", raising=False)


def _reload_config():
    import src.config as config_module

    return importlib.reload(config_module)


def test_default_is_windows_exe_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    config_module = _reload_config()
    settings = config_module.Settings()
    assert settings.tdl_binary_path == "./tools/tdl/tdl.exe"


def test_default_is_bare_binary_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    config_module = _reload_config()
    settings = config_module.Settings()
    assert settings.tdl_binary_path == "./tools/tdl/tdl"


def test_env_override_wins_regardless_of_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("TDL_BINARY_PATH", "/custom/path/tdl-custom")
    config_module = _reload_config()
    settings = config_module.Settings()
    assert settings.tdl_binary_path == "/custom/path/tdl-custom"
