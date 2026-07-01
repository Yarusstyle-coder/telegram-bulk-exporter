"""Unit tests for src.services.tdl_retry."""

from __future__ import annotations

import pytest

from src.services.tdl_retry import FloodWaitNotRetried, with_retry
from src.services.tdl_types import TdlError
from src.services.tdl_wrapper import TdlSubprocessError


def _make_err(kind: str, wait: int | None = None, msg: str = "") -> TdlSubprocessError:
    tdl_err = TdlError(kind=kind, message=msg or kind, wait_seconds=wait)  # type: ignore[arg-type]
    return TdlSubprocessError(
        argv=["tdl", "x"],
        returncode=1,
        stdout="",
        stderr=msg or kind,
        errors=[tdl_err],
    )


@pytest.mark.asyncio
async def test_success_after_two_network_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("src.services.tdl_retry.asyncio.sleep", no_sleep)

    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _make_err("network", msg="connection reset")
        return "ok"

    result = await with_retry(flaky, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_stops_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("src.services.tdl_retry.asyncio.sleep", no_sleep)

    calls = {"n": 0}

    async def always_fail() -> None:
        calls["n"] += 1
        raise _make_err("network", msg="boom")

    with pytest.raises(TdlSubprocessError):
        await with_retry(always_fail, max_attempts=3, base_delay=0.0)
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_flood_wait_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("src.services.tdl_retry.asyncio.sleep", no_sleep)

    calls = {"n": 0}

    async def floods() -> None:
        calls["n"] += 1
        raise _make_err("flood_wait", wait=30, msg="FLOOD_WAIT_X 30")

    with pytest.raises(FloodWaitNotRetried) as excinfo:
        await with_retry(floods, max_attempts=5, base_delay=0.0)

    assert excinfo.value.wait_seconds == 30
    assert calls["n"] == 1  # no retry


@pytest.mark.asyncio
async def test_generic_exception_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-classified exceptions still get retried up to max_attempts."""

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("src.services.tdl_retry.asyncio.sleep", no_sleep)

    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        return "ok"

    result = await with_retry(flaky, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls["n"] == 2
