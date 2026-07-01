"""Line-oriented parser for `tdl` subprocess output.

`tdl` prints progress as a rendered progress bar (via `mpb`) plus a handful
of structured log lines (via `zap` with a `cli` encoder). The concrete
format has drifted between releases, so this module keeps every regex in a
single swappable :data:`PATTERNS` table. A future tdl update that tweaks
spacing should only require touching regexes here.

Conservative tolerances applied by :class:`ProgressParser.feed`:

* strips ANSI color / cursor escapes before matching;
* ignores unicode spinner frames (`|`, `/`, `-`, `\\`, braille frames);
* returns ``None`` for any line that doesn't match a known frame.

Typical stdout/stderr frames we recognise (synthesised from `tdl` Go
source behaviour — `mpb.BarStyle("[=>-]")`-ish renders and tdl's own
`logger` package):

```
 42%  123/456  12.3 MiB/s  eta 1m23s  photo_123.jpg
EXPORTING: 17%  17/100
DOWNLOADING   27%  12.3 MB/s  eta 1m23s
ERRO    flood wait (15s)
ERROR: FLOOD_WAIT_X 15
ERRO    network error: connection reset
```

The regexes are deliberately permissive — real progress bars include
block-drawing characters, padding spaces, and sometimes trailing
byte-count annotations. Tests in ``tests/unit/test_tdl_progress_parser``
cover the cases we have seen.
"""

from __future__ import annotations

import re

from src.services.tdl_types import TdlError, TdlProgress

# ---------------------------------------------------------------------------
# ANSI stripping — the tdl renderer includes cursor moves and color codes.
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SPINNER_CHARS = set("|/-\\" + "".join(chr(c) for c in range(0x2800, 0x2900)))


def _strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line)


def _strip_spinner(line: str) -> str:
    # Remove standalone spinner glyphs (not the ones that happen to be inside
    # filenames — we only nuke contiguous runs at the start).
    i = 0
    while i < len(line) and (line[i].isspace() or line[i] in _SPINNER_CHARS):
        i += 1
    return line[i:]


# ---------------------------------------------------------------------------
# Unit parsing helpers
# ---------------------------------------------------------------------------
_UNIT_MULT = {
    "b": 1,
    "kb": 1_000,
    "kib": 1_024,
    "mb": 1_000_000,
    "mib": 1_048_576,
    "gb": 1_000_000_000,
    "gib": 1_073_741_824,
}


def _to_bps(value: str, unit: str) -> int:
    mult = _UNIT_MULT.get(unit.lower().rstrip("/s").rstrip(), 1)
    try:
        return int(float(value) * mult)
    except ValueError:
        return 0


def _to_seconds(expr: str) -> int:
    """Parse '1m23s', '45s', '2h3m10s' → seconds."""
    total = 0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)([hms])", expr):
        n = float(num)
        if unit == "h":
            total += int(n * 3600)
        elif unit == "m":
            total += int(n * 60)
        else:
            total += int(n)
    return total


# ---------------------------------------------------------------------------
# Pattern table — swap these when tdl output format drifts.
# ---------------------------------------------------------------------------
PATTERNS: dict[str, re.Pattern[str]] = {
    # Progress frames: e.g.
    #   " 27%  123/456  12.3 MiB/s  eta 1m23s  photo.jpg"
    "progress_full": re.compile(
        r"""
        ^\s*
        (?:(?P<stage>EXPORTING|DOWNLOADING|UPLOADING)\s*:?\s*)?
        (?P<percent>\d+(?:\.\d+)?)\s*%\s+
        (?P<current>\d+)\s*/\s*(?P<total>\d+)
        (?:\s+(?P<speed>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]?i?B/?s?))?
        (?:\s+eta\s+(?P<eta>[\dhms.]+))?
        (?:\s+(?P<file>.+?))?
        \s*$
        """,
        re.VERBOSE | re.IGNORECASE,
    ),
    # Progress without a numeric counter: "DOWNLOADING 27% 12.3 MB/s eta 1m23s"
    "progress_percent_only": re.compile(
        r"""
        ^\s*
        (?P<stage>EXPORTING|DOWNLOADING|UPLOADING)\s*:?\s*
        (?P<percent>\d+(?:\.\d+)?)\s*%
        (?:\s+(?P<speed>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]?i?B/?s?))?
        (?:\s+eta\s+(?P<eta>[\dhms.]+))?
        (?:\s+(?P<file>.+?))?
        \s*$
        """,
        re.VERBOSE | re.IGNORECASE,
    ),
    # "EXPORTING ... done" / "DOWNLOADING ... done"
    "stage_done": re.compile(
        r"^\s*(?P<stage>EXPORTING|DOWNLOADING|UPLOADING)\b.*\bdone\b",
        re.IGNORECASE,
    ),
    # Flood wait: multiple forms tdl / Telegram may emit.
    "flood_wait": re.compile(
        r"FLOOD_?WAIT(?:_X)?\s*[:=]?\s*(?P<sec>\d+)",
        re.IGNORECASE,
    ),
    "flood_wait_parens": re.compile(
        r"flood\s*wait\s*\(\s*(?P<sec>\d+)\s*s?\s*\)",
        re.IGNORECASE,
    ),
    # Generic auth errors.
    "auth_error": re.compile(
        r"\b(AUTH_KEY_UNREGISTERED|SESSION_REVOKED|USER_DEACTIVATED|"
        r"auth(?:enticat(?:e|ion))?\s+(?:failed|error|required)|"
        r"not\s+logged\s+in)\b",
        re.IGNORECASE,
    ),
    # Generic network errors.
    "network_error": re.compile(
        r"\b(connection\s+(?:reset|refused|timed?\s*out)|network\s+error|"
        r"dial\s+tcp|EOF|i/o\s+timeout|no\s+such\s+host)\b",
        re.IGNORECASE,
    ),
    # Generic ERRO/ERROR level line from tdl's zap encoder.
    "error_level": re.compile(
        r"^\s*(?:ERRO(?:R)?|FATAL)\b[:\s]\s*(?P<msg>.+?)\s*$",
        re.IGNORECASE,
    ),
}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
class ProgressParser:
    """Stateful line-by-line parser.

    Feed every stdout/stderr line through :meth:`feed`. It returns one of:

    * :class:`TdlProgress` — a recognised progress frame,
    * :class:`TdlError` — a recognised error/flood line,
    * ``None`` — line was blank / unknown / spinner-only.

    The parser is stateful only in the sense that it remembers the last
    inferred stage; if a line matches a percent-only frame without an
    explicit stage word, the last-seen stage is reused.
    """

    def __init__(self, default_stage: str = "download") -> None:
        self._last_stage: str = default_stage

    # ------------------------------------------------------------------
    def feed(self, line: str) -> TdlProgress | TdlError | None:
        if not line:
            return None
        clean = _strip_spinner(_strip_ansi(line)).rstrip()
        if not clean:
            return None

        # Flood wait takes priority — it's a retriable but special-case
        # error that the caller must surface as a countdown, not a retry.
        if (m := PATTERNS["flood_wait"].search(clean)) or (
            m := PATTERNS["flood_wait_parens"].search(clean)
        ):
            sec = int(m.group("sec"))
            return TdlError(
                kind="flood_wait",
                message=clean.strip(),
                wait_seconds=sec,
            )

        if PATTERNS["auth_error"].search(clean):
            return TdlError(kind="auth", message=clean.strip())

        if PATTERNS["network_error"].search(clean):
            return TdlError(kind="network", message=clean.strip())

        if m := PATTERNS["progress_full"].match(clean):
            return self._make_progress(m, with_counter=True)

        if m := PATTERNS["progress_percent_only"].match(clean):
            return self._make_progress(m, with_counter=False)

        if m := PATTERNS["stage_done"].match(clean):
            self._last_stage = self._normalize_stage(m.group("stage"))
            return None  # a "done" marker isn't itself a progress frame

        if m := PATTERNS["error_level"].match(clean):
            msg = m.group("msg")
            # Re-run the flood / auth / network classifiers on the inner msg.
            inner: str = msg
            if n := PATTERNS["flood_wait"].search(inner):
                return TdlError(
                    kind="flood_wait", message=inner, wait_seconds=int(n.group("sec"))
                )
            if PATTERNS["auth_error"].search(inner):
                return TdlError(kind="auth", message=inner)
            if PATTERNS["network_error"].search(inner):
                return TdlError(kind="network", message=inner)
            return TdlError(kind="unknown", message=inner)

        return None

    # ------------------------------------------------------------------
    def _make_progress(
        self, m: re.Match[str], *, with_counter: bool
    ) -> TdlProgress:
        stage_raw = m.groupdict().get("stage")
        stage = (
            self._normalize_stage(stage_raw) if stage_raw else self._last_stage
        )
        self._last_stage = stage

        percent = float(m.group("percent"))
        if with_counter:
            current = int(m.group("current"))
            total = int(m.group("total"))
        else:
            # percent-only: project onto 0..100
            current = int(percent)
            total = 100

        speed = m.groupdict().get("speed")
        unit = m.groupdict().get("unit")
        speed_bps = _to_bps(speed, unit) if speed and unit else None

        eta_expr = m.groupdict().get("eta")
        eta_seconds = _to_seconds(eta_expr) if eta_expr else None

        file = m.groupdict().get("file") or None
        if file is not None:
            file = file.strip()
            if not file:
                file = None

        return TdlProgress(
            stage=stage,  # type: ignore[arg-type]
            current=current,
            total=total,
            percent=percent,
            speed_bps=speed_bps,
            eta_seconds=eta_seconds,
            file=file,
        )

    @staticmethod
    def _normalize_stage(raw: str) -> str:
        raw = raw.upper()
        if raw.startswith("EXPORT"):
            return "export"
        # Treat DOWNLOADING and UPLOADING as "download" stage for the
        # public enum — uploads aren't in this phase.
        return "download"
