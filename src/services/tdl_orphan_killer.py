"""Kill stray tdl.exe processes locking our bolt-DB before we spawn new ones.

Why this exists
---------------
tdl's bolt-DB is single-writer. When a sync job is cancelled and the
runner's `proc.kill()` somehow fails to actually kill the tdl child
(Windows process tree quirks, abrupt server crash, etc.), the orphan
process keeps the bolt file locked. Every subsequent sync attempt then
fails immediately with::

    Current database is used by another process, please terminate it first

…and the user sees a string of FAILED jobs whose root cause is invisible
unless they go process-hunting. We solve this by sweeping the OS process
table at server boot and after every cancel: any `tdl.exe` whose command
line points at *our* storage path gets a forced kill. Matching by the
`--storage path=<our_path>` arg means we never touch tdl invocations
the user launched outside our control.

The implementation uses Windows `Get-CimInstance Win32_Process` via
PowerShell because we don't want to pull in `psutil` for a single
boot-time sweep. On non-Windows we fall back to ``/proc`` scanning.
Either way the function NEVER raises — a failure to enumerate or kill
is logged and the caller keeps booting; the worst case is the user
sees the original lock error.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from src.logging_setup import get_logger

log = get_logger(__name__)


def _normalise(path: str | Path) -> str:
    """Lower-case, forward-slash form so command-line + path strings
    compare regardless of how Windows quoted them."""
    return str(path).replace("\\", "/").lower()


def kill_orphan_tdl(storage_path: Path) -> int:
    """Kill every ``tdl.exe`` (or ``tdl``) holding *our* bolt-DB.

    Returns the count of processes terminated. Best-effort: errors are
    logged and the function returns 0 rather than raising. Skips
    processes whose command line does NOT contain the configured
    storage path, so unrelated tdl invocations remain untouched.
    """
    try:
        marker = _normalise(storage_path)
        if sys.platform == "win32":
            killed = _kill_windows(marker)
        else:
            killed = _kill_posix(marker)
        if killed:
            log.info("tdl_orphans_killed", count=killed, marker=marker)
        return killed
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("tdl_orphan_sweep_failed", error=str(exc))
        return 0


# ---------------------------------------------------------------------------


def _kill_windows(marker: str) -> int:
    """Windows path: shell out to PowerShell once, kill in-process."""
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if pwsh is None:
        log.debug("tdl_orphan_sweep_skipped", reason="no powershell on PATH")
        return 0
    cmd = [
        pwsh,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        # CSV would be lighter but JSON survives Russian Windows code-
        # page conversions more cleanly. -Compress keeps one JSON line.
        "Get-CimInstance Win32_Process -Filter \"Name='tdl.exe'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("tdl_orphan_ps_failed", error=str(exc))
        return 0
    if proc.returncode != 0:
        log.warning(
            "tdl_orphan_ps_nonzero",
            rc=proc.returncode,
            stderr=(proc.stderr or "")[:200],
        )
        return 0
    raw = (proc.stdout or "").strip()
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("tdl_orphan_ps_unparseable", error=str(exc), raw=raw[:200])
        return 0
    if isinstance(data, dict):
        data = [data]  # single-process result is unwrapped
    killed = 0
    for entry in data:
        cmdline = _normalise(entry.get("CommandLine") or "")
        if marker not in cmdline:
            continue
        pid = entry.get("ProcessId")
        if not isinstance(pid, int):
            continue
        try:
            os.kill(pid, 9)  # TerminateProcess on Windows
            killed += 1
            log.info("tdl_orphan_killed", pid=pid)
        except OSError as exc:
            log.warning("tdl_orphan_kill_failed", pid=pid, error=str(exc))
    return killed


def _kill_posix(marker: str) -> int:
    """Linux/macOS path: walk ``/proc`` (Linux) or use ``ps`` fallback."""
    proc_dir = Path("/proc")
    candidates: list[tuple[int, str]] = []
    if proc_dir.exists():
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().decode(
                    "utf-8", errors="replace"
                )
            except OSError:
                continue
            if "tdl" in entry.name or "tdl" in cmdline:
                candidates.append((int(entry.name), cmdline))
    else:
        # macOS — fall back to ``ps``.
        try:
            out = subprocess.run(
                ["ps", "-axww", "-o", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return 0
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            pid_str, _, cmd = line.partition(" ")
            try:
                candidates.append((int(pid_str), cmd))
            except ValueError:
                continue

    killed = 0
    for pid, cmdline in candidates:
        norm = _normalise(cmdline)
        if "tdl" not in norm:
            continue
        if marker not in norm:
            continue
        try:
            os.kill(pid, 9)
            killed += 1
            log.info("tdl_orphan_killed", pid=pid)
        except OSError as exc:
            log.warning("tdl_orphan_kill_failed", pid=pid, error=str(exc))
    return killed
