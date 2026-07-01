"""tdl login session manager — runs `tdl login -T qr` in background and
exposes its state to the UI.

tdl maintains its own session storage independent of Telethon. Without
running `tdl login` once, every `tdl chat export` / `tdl dl` fails with
'not authorized. please login first'. We wrap the QR-mode login flow:

1. Spawn `tdl login -T qr` as a subprocess.
2. Pump stdout. tdl prints "Scan QR code with your Telegram app..."
   followed by the QR rendered as Unicode block characters (one frame
   every ~30 s, with ANSI cursor-up sequences between frames).
3. Track state — STARTING → WAITING_FOR_SCAN → SUCCESS / ERROR / TIMEOUT.
4. When the user scans, tdl writes "Login successfully!" and exits 0.
   We mark SUCCESS; subsequent `tdl chat export` calls now have a
   working session.

`desktop` mode is also supported: tdl reads tdata from an installed
Telegram Desktop and is done in a second; no QR involved.
"""

from __future__ import annotations

import asyncio
import enum
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from src.logging_setup import get_logger

log = get_logger(__name__)


class TdlLoginState(str, enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    WAITING = "waiting"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class TdlLoginStatus:
    state: TdlLoginState = TdlLoginState.IDLE
    qr_lines: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    def to_json(self) -> dict:
        return {
            "state": self.state.value,
            "qr_lines": list(self.qr_lines),
            "log_lines": list(self.log_lines[-20:]),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ASCII Block / half-block characters tdl uses to render QR in the terminal.
_QR_BLOCK_CHARS = "█▀▄▌▐ "
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SUCCESS_RE = re.compile(r"login successfully|signed in", re.IGNORECASE)
_FAIL_RE = re.compile(r"failed|error", re.IGNORECASE)


def _is_qr_line(line: str) -> bool:
    """A line that is mostly QR block characters."""
    if not line:
        return False
    blocks = sum(1 for c in line if c in _QR_BLOCK_CHARS)
    return blocks / max(1, len(line)) > 0.5


class TdlLoginManager:
    """One in-flight tdl login process. The UI polls `status()`.

    Only one login may be in flight at a time per namespace. If a second
    `start()` call comes while a process is running, we return the existing
    status object instead of spawning twice.
    """

    def __init__(
        self,
        *,
        binary_path: Path,
        namespace: str = "default",
        storage_path: Path | None = None,
    ) -> None:
        self._binary = binary_path
        self._namespace = namespace
        self._storage_path = storage_path
        self._proc: asyncio.subprocess.Process | None = None
        self._status = TdlLoginStatus()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def status(self) -> TdlLoginStatus:
        # Cheap probe — if the bolt-DB has session bytes for this namespace,
        # tdl is authorised regardless of whether *we* spawned the login.
        # Lets a CLI `tdl login` run from PowerShell flip the UI to SUCCESS.
        if (
            self._status.state in (TdlLoginState.IDLE, TdlLoginState.WAITING)
            and self._has_existing_session()
        ):
            self._status.state = TdlLoginState.SUCCESS
            self._status.finished_at = self._status.finished_at or time.time()
        return self._status

    def _has_existing_session(self) -> bool:
        """tdl stores its session at `~/.tdl/data/<namespace>` — either as a
        single bolt-DB file (default in 0.20+) or as a directory of files
        (legacy / 'file' driver). Either form with nonzero content is
        treated as an authorised session.
        Also checks the configured local storage_path."""
        from pathlib import Path

        candidates: list[Path] = [
            Path.home() / ".tdl" / "data" / self._namespace,
            Path.home() / ".tdl" / "data" / "default",
        ]
        if self._storage_path is not None:
            candidates.insert(0, self._storage_path / self._namespace)
            candidates.insert(0, self._storage_path / "default")
        for root in candidates:
            try:
                if not root.exists():
                    continue
                if root.is_file() and root.stat().st_size > 0:
                    return True
                if root.is_dir():
                    for p in root.rglob("*"):
                        if p.is_file() and p.stat().st_size > 0:
                            return True
            except OSError:
                continue
        return False

    def is_running(self) -> bool:
        sp = getattr(self, "_sync_proc", None)
        if sp is not None and sp.poll() is None:
            return True
        return self._proc is not None and self._proc.returncode is None

    async def cancel(self) -> bool:
        async with self._lock:
            sp = getattr(self, "_sync_proc", None)
            if sp is not None and sp.poll() is None:
                try:
                    sp.terminate()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    sp.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    with _suppress(Exception):
                        sp.kill()
                self._status.state = TdlLoginState.CANCELLED
                self._status.finished_at = time.time()
                return True
            if self._proc is not None and self._proc.returncode is None:
                try:
                    self._proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3.0)
                except TimeoutError:
                    with _suppress(Exception):
                        self._proc.kill()
                self._status.state = TdlLoginState.CANCELLED
                self._status.finished_at = time.time()
                return True
            return False

    async def start(self, *, mode: str = "qr", desktop_passcode: str | None = None) -> TdlLoginStatus:
        """Spawn `tdl login -T <mode>`.

        Two flavours depending on mode:

        - **qr / code**: tdl is highly interactive (wants a TTY to draw the
          live-updating QR or read a code from stdin). On Windows it goes
          mute when stdout is a pipe. We launch in a *new console window*
          (`CREATE_NEW_CONSOLE`) so tdl gets a real TTY; the user scans /
          types in that window and we just watch the exit code.

        - **desktop**: completely non-interactive. We pipe stdout/stderr
          and pump output via a background thread for live status.
        """
        import subprocess
        import threading

        async with self._lock:
            # Allow re-trigger if a previous run died but is_running() still
            # thinks it's alive (e.g. crashed pump thread).
            if self.is_running():
                # Force-close the old handle if it's actually dead.
                sp = getattr(self, "_sync_proc", None)
                if sp is not None and sp.poll() is not None:
                    self._sync_proc = None
                else:
                    return self._status

            self._status = TdlLoginStatus(
                state=TdlLoginState.STARTING,
                started_at=time.time(),
            )

            argv: list[str] = [str(self._binary), "-n", self._namespace]
            if self._storage_path is not None:
                argv += ["--storage", f"type=bolt,path={self._storage_path.as_posix()}"]
            argv += ["login", "-T", mode]
            if mode == "desktop" and desktop_passcode:
                argv.extend(["-p", desktop_passcode])

            log.info("tdl_login_spawn", argv=argv[1:], mode=mode)

            # Decide TTY vs piped based on the mode.
            interactive = mode in ("qr", "code")
            popen_kwargs: dict = {
                "stdin": subprocess.DEVNULL if not interactive else None,
            }
            if interactive and hasattr(subprocess, "CREATE_NEW_CONSOLE"):
                # New console window so tdl draws QR / reads code in a real TTY.
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
                # No piping — we cannot poll output from a separate console.
                popen_kwargs["stdout"] = None
                popen_kwargs["stderr"] = None
                self._status.log_lines.append(
                    "Открылось отдельное окно tdl. В нём появится QR-код / запрос кода. "
                    "После авторизации это окно закроется само."
                )
            else:
                popen_kwargs["stdout"] = subprocess.PIPE
                popen_kwargs["stderr"] = subprocess.STDOUT
                popen_kwargs["text"] = True
                popen_kwargs["encoding"] = "utf-8"
                popen_kwargs["errors"] = "replace"
                popen_kwargs["bufsize"] = 1

            try:
                self._sync_proc = subprocess.Popen(argv, **popen_kwargs)  # noqa: S603
            except FileNotFoundError as exc:
                self._status.state = TdlLoginState.ERROR
                self._status.error = f"tdl binary not found: {exc}"
                return self._status

            self._status.state = TdlLoginState.WAITING
            self._proc = None
            target = self._pump_sync if not interactive else self._wait_for_exit
            self._pump_thread = threading.Thread(
                target=target, name=f"tdl-login-pump-{mode}", daemon=True
            )
            self._pump_thread.start()
        return self._status

    def _wait_for_exit(self) -> None:
        """For interactive (new-console) mode: just wait for tdl to exit and
        translate the exit code into a final state. Output goes to the user's
        console window, not to us."""
        proc = getattr(self, "_sync_proc", None)
        if proc is None:
            return
        try:
            rc = proc.wait()
        except Exception as exc:  # noqa: BLE001
            log.warning("tdl_login_wait_failed", error=str(exc))
            self._status.state = TdlLoginState.ERROR
            self._status.error = str(exc)
            self._sync_proc = None
            return
        self._status.finished_at = time.time()
        if rc == 0:
            self._status.state = TdlLoginState.SUCCESS
            log.info("tdl_login_success", mode="interactive")
        elif self._status.state is not TdlLoginState.CANCELLED:
            self._status.state = TdlLoginState.ERROR
            self._status.error = f"tdl login exited with code {rc}"
            log.warning("tdl_login_failed", rc=rc)
        self._sync_proc = None

    def _pump_sync(self) -> None:
        proc = getattr(self, "_sync_proc", None)
        if proc is None or proc.stdout is None:
            return

        current_frame: list[str] = []
        published = False

        try:
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                line = _ANSI_CSI_RE.sub("", line)
                if not line.strip():
                    continue

                self._status.log_lines.append(line)

                lower = line.lower()
                if "scan qr" in lower:
                    self._status.state = TdlLoginState.WAITING
                    current_frame = []
                    published = False
                    continue

                if _is_qr_line(line):
                    current_frame.append(line)
                    if len(current_frame) >= 10 and not published:
                        self._status.qr_lines = list(current_frame)
                        published = True
                    elif published and len(current_frame) > len(self._status.qr_lines):
                        self._status.qr_lines = list(current_frame)
                    continue

                # Non-block line after blocks: end of a QR frame; commit.
                if current_frame:
                    self._status.qr_lines = list(current_frame)
                    current_frame = []
                    published = True

                if _SUCCESS_RE.search(line):
                    self._status.state = TdlLoginState.SUCCESS
                elif _FAIL_RE.search(line) and self._status.state is TdlLoginState.WAITING:
                    self._status.error = line
                    self._status.state = TdlLoginState.ERROR

            rc = proc.wait()
            self._status.finished_at = time.time()
            if rc == 0 and self._status.state is not TdlLoginState.CANCELLED:
                self._status.state = TdlLoginState.SUCCESS
                log.info("tdl_login_success")
            elif self._status.state not in (TdlLoginState.SUCCESS, TdlLoginState.CANCELLED):
                self._status.state = TdlLoginState.ERROR
                self._status.error = self._status.error or f"tdl login exited with code {rc}"
                log.warning("tdl_login_failed", rc=rc, error=self._status.error)
        except Exception as exc:  # noqa: BLE001
            log.exception("tdl_login_pump_crashed", error=str(exc))
            self._status.state = TdlLoginState.ERROR
            self._status.error = str(exc)
        finally:
            self._sync_proc = None


class _suppress:  # tiny contextlib.suppress shim that doesn't import contextlib
    def __init__(self, *exc: type[BaseException]) -> None:
        self._exc = exc

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc_val, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, self._exc)


def list_modes() -> Iterable[tuple[str, str]]:
    return [
        ("qr", "QR-код (сканируйте через Telegram → Settings → Devices → Link Desktop Device)"),
        ("desktop", "Импорт из Telegram Desktop (если установлен и залогинен)"),
        ("code", "SMS-код (как у Telethon)"),
    ]
