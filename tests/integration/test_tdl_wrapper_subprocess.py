"""Integration test — actually spawns `tdl version` through the wrapper.

This test is deliberately offline: `tdl version` inspects the binary
only and never touches Telegram's network. If the binary is missing the
test is skipped so CI doesn't fail when the bundled tool isn't shipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.services.tdl_wrapper import TdlBinaryMissingError, TdlWrapper

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BINARY = REPO_ROOT / "tools" / "tdl" / "tdl.exe"


@pytest.mark.asyncio
async def test_version_parses_real_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not DEFAULT_BINARY.exists():
        pytest.skip(f"tdl binary not found at {DEFAULT_BINARY}")

    monkeypatch.setenv("TDL_BINARY_PATH", str(DEFAULT_BINARY))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    settings = Settings()

    assert Path(settings.tdl_binary_path).resolve() == DEFAULT_BINARY.resolve()

    wrapper = TdlWrapper(settings=settings)
    version = await wrapper.version()
    assert version.startswith("v"), f"expected vMAJOR.MINOR.PATCH, got {version!r}"
    # Must match the known shape vX.Y.Z (allow pre-release suffixes).
    parts = version[1:].split(".")
    assert len(parts) >= 3
    assert parts[0].isdigit()
    assert parts[1].isdigit()


def test_missing_binary_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bogus = tmp_path / "does" / "not" / "exist.exe"
    monkeypatch.setenv("TDL_BINARY_PATH", str(bogus))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))
    settings = Settings()

    with pytest.raises(TdlBinaryMissingError):
        TdlWrapper(settings=settings)
