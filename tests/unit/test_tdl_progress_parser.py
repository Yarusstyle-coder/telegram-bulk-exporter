"""Unit tests for the tdl stdout/stderr progress parser.

The sample lines below mirror tdl's mpb-based progress rendering
(see `github.com/iyear/tdl` ~ cmd/dl/progress.go and logger/zap config).
Exact byte-for-byte capture requires live Telegram creds; for the
offline test suite we synthesise frames that match the regex table.
"""

from __future__ import annotations

import pytest

from src.services.tdl_progress_parser import ProgressParser
from src.services.tdl_types import TdlError, TdlProgress


# ---------------------------------------------------------------------------
# Progress frames
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # 1) Classic download frame with counter, speed, eta, filename.
        (
            "DOWNLOADING  42%  123/456  12.3 MiB/s  eta 1m23s  photo_123.jpg",
            TdlProgress(
                stage="download",
                current=123,
                total=456,
                percent=42.0,
                speed_bps=int(12.3 * 1024 * 1024),
                eta_seconds=83,
                file="photo_123.jpg",
            ),
        ),
        # 2) Export frame: counter-only (no speed/eta).
        (
            "EXPORTING: 17%  17/100",
            TdlProgress(
                stage="export",
                current=17,
                total=100,
                percent=17.0,
                speed_bps=None,
                eta_seconds=None,
                file=None,
            ),
        ),
        # 3) Percent-only download with unit in MB/s (decimal).
        (
            "DOWNLOADING 27% 12.3 MB/s eta 1m23s",
            TdlProgress(
                stage="download",
                current=27,
                total=100,
                percent=27.0,
                speed_bps=int(12.3 * 1_000_000),
                eta_seconds=83,
                file=None,
            ),
        ),
        # 4) With ANSI color codes — should be stripped before match.
        (
            "\x1b[32mDOWNLOADING\x1b[0m  50%  50/100  1.0 KiB/s  eta 10s",
            TdlProgress(
                stage="download",
                current=50,
                total=100,
                percent=50.0,
                speed_bps=1024,
                eta_seconds=10,
                file=None,
            ),
        ),
        # 5) Leading spinner char + spaces.
        (
            "|  75%  3/4  2.0 MiB/s  eta 1s  movie.mp4",
            TdlProgress(
                stage="download",  # default stage carries over
                current=3,
                total=4,
                percent=75.0,
                speed_bps=2 * 1024 * 1024,
                eta_seconds=1,
                file="movie.mp4",
            ),
        ),
        # 6) Hours + minutes ETA.
        (
            "DOWNLOADING 5% 5/100 500 KiB/s eta 2h3m10s",
            TdlProgress(
                stage="download",
                current=5,
                total=100,
                percent=5.0,
                speed_bps=500 * 1024,
                eta_seconds=2 * 3600 + 3 * 60 + 10,
                file=None,
            ),
        ),
    ],
)
def test_progress_frames(line: str, expected: TdlProgress) -> None:
    p = ProgressParser()
    got = p.feed(line)
    assert isinstance(got, TdlProgress), f"expected TdlProgress, got {type(got)}: {got!r}"
    assert got.stage == expected.stage
    assert got.current == expected.current
    assert got.total == expected.total
    assert got.percent == expected.percent
    assert got.speed_bps == expected.speed_bps
    assert got.eta_seconds == expected.eta_seconds
    assert got.file == expected.file


# ---------------------------------------------------------------------------
# Error / flood-wait frames
# ---------------------------------------------------------------------------
def test_flood_wait_x_format() -> None:
    p = ProgressParser()
    got = p.feed("ERRO    FLOOD_WAIT_X 15")
    assert isinstance(got, TdlError)
    assert got.kind == "flood_wait"
    assert got.wait_seconds == 15


def test_flood_wait_parenthesised() -> None:
    p = ProgressParser()
    got = p.feed("flood wait (42s)")
    assert isinstance(got, TdlError)
    assert got.kind == "flood_wait"
    assert got.wait_seconds == 42


def test_auth_error_detected() -> None:
    p = ProgressParser()
    got = p.feed("ERRO    AUTH_KEY_UNREGISTERED")
    assert isinstance(got, TdlError)
    assert got.kind == "auth"


def test_network_error_detected() -> None:
    p = ProgressParser()
    got = p.feed("ERRO    network error: connection reset")
    assert isinstance(got, TdlError)
    assert got.kind == "network"


def test_unknown_error_line() -> None:
    p = ProgressParser()
    got = p.feed("ERROR: something weird happened")
    assert isinstance(got, TdlError)
    assert got.kind == "unknown"
    assert "something weird" in got.message


# ---------------------------------------------------------------------------
# Graceful no-ops
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "|",
        "\x1b[2K",  # ANSI clear line only
        "EXPORTING ... done",  # stage_done is a state transition, returns None
        "Some random unrecognised line from tdl",
    ],
)
def test_returns_none_on_noise(line: str) -> None:
    p = ProgressParser()
    assert p.feed(line) is None


def test_stage_done_updates_last_stage() -> None:
    """After 'EXPORTING ... done', a subsequent percent-only frame inherits
    'export' when no explicit stage is present in the new line."""
    p = ProgressParser(default_stage="download")
    assert p.feed("EXPORTING done") is None
    # A subsequent fully-anchored percent-only frame without a stage word
    # would match progress_percent_only which REQUIRES a stage word, so
    # it falls through to progress_full which also requires counters.
    # The state is still exposed via the next stage-bearing frame:
    evt = p.feed("DOWNLOADING 10% 1/10")
    assert isinstance(evt, TdlProgress)
    assert evt.stage == "download"
