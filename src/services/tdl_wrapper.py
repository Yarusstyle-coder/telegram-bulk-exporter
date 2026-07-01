"""Async subprocess wrapper around the bundled ``tdl`` binary.

Design goals
------------
* ``async`` everywhere — no ``subprocess.run``. We consume stdout/stderr
  as two parallel tasks so progress can be emitted while the child is
  still running.
* ``shell=False`` always — argv is an explicit list.
* Callable argv assembly (:func:`build_argv`) so tests can assert
  exactly which flags we pass for a given call.
* Callbacks, not queues — a caller passes ``on_progress`` at construction
  (or per-call override) to receive :class:`TdlProgress` /
  :class:`TdlError` frames as they arrive.
* Sensible timeouts: 10 s for ``version``, unbounded for downloads (the
  orchestrator owns cancellation policy — we cancel cleanly when its
  task is cancelled).

This file is the boundary with the Go binary; no other module should
shell out to tdl directly.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from src.config import Settings, get_settings
from src.logging_setup import get_logger
from src.services.tdl_progress_parser import ProgressParser
from src.services.tdl_types import ChatExportResult, DlResult, TdlError, TdlProgress

log = get_logger(__name__)

ProgressCallback = Callable[[TdlProgress | TdlError], None]


# ---------------------------------------------------------------------------
# argv assembly — isolated so tests can assert on it without spawning.
# ---------------------------------------------------------------------------
def build_argv(
    binary: Path,
    subcommand: Iterable[str],
    *,
    namespace: str | None = None,
    proxy: str | None = None,
    storage_path: Path | None = None,
    extra: Iterable[str] = (),
) -> list[str]:
    """Return the concrete argv list passed to ``asyncio.create_subprocess_exec``.

    Global flags (``-n``, ``--proxy``, ``--storage``) go before the subcommand,
    per tdl's cobra layout. ``subcommand`` is the verb path (e.g.
    ``["chat", "ls"]``). ``extra`` is the subcommand-specific flag list.
    """
    argv: list[str] = [str(binary)]
    if namespace:
        argv += ["-n", namespace]
    if proxy:
        argv += ["--proxy", proxy]
    if storage_path is not None:
        argv += ["--storage", f"type=bolt,path={storage_path.as_posix()}"]
    argv += list(subcommand)
    argv += list(extra)
    return argv


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class TdlBinaryMissingError(FileNotFoundError):
    """Raised when the configured tdl binary does not exist at init."""


class TdlSubprocessError(RuntimeError):
    """Raised when a tdl invocation exits non-zero."""

    def __init__(
        self,
        argv: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
        errors: list[TdlError] | None = None,
    ) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.errors: list[TdlError] = errors or []
        summary = stderr.strip().splitlines()[-1:] or stdout.strip().splitlines()[-1:]
        super().__init__(
            f"tdl {argv[1:]!r} exited {returncode}: {summary[0] if summary else '(no output)'}"
        )


# ---------------------------------------------------------------------------
# Main wrapper
# ---------------------------------------------------------------------------
class TdlWrapper:
    """Async class-based API over the tdl binary."""

    # Override via tests if needed.
    VERSION_TIMEOUT: float = 10.0

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        namespace: str | None = None,
        proxy: str | None = None,
        on_progress: ProgressCallback | None = None,
        binary_path: Path | None = None,
    ) -> None:
        s = settings or get_settings()
        self._settings = s
        self._namespace = namespace if namespace is not None else s.tdl_namespace
        self._proxy = proxy if proxy is not None else s.proxy

        # Override tdl's default ~/.tdl/data/<ns> bolt storage to our DATA_DIR
        # so everything (sessions, peer cache, dialog metadata) lives on the
        # user's chosen drive together with the rest of the project state.
        self._storage_path: Path | None = None
        if getattr(s, "tdl_use_local_storage", False):
            self._storage_path = s.tdl_storage_dir
            self._storage_path.mkdir(parents=True, exist_ok=True)

        raw = binary_path or Path(s.tdl_binary_path)
        self._binary = raw if raw.is_absolute() else raw.resolve()
        if not self._binary.exists():
            raise TdlBinaryMissingError(
                f"tdl binary not found at {self._binary} (configured: {s.tdl_binary_path!r})"
            )

        # Bolt-DB is single-writer per file. Serialise calls with an
        # asyncio.Lock so concurrent chat_export / dl / chat_ls don't race
        # and trigger 'database is used by another process'.
        import asyncio as _asyncio

        self._db_lock = _asyncio.Lock()
        self._on_progress = on_progress

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def binary(self) -> Path:
        return self._binary

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def proxy(self) -> str | None:
        return self._proxy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def version(self) -> str:
        """Return the tdl tag, e.g. ``'v0.20.2'`` (v-prefix normalised)."""
        argv = build_argv(
            self._binary,
            ["version"],
            namespace=None,  # version doesn't need these and some versions reject them
            proxy=None,
        )
        stdout, stderr, rc = await self._run_simple(argv, timeout=self.VERSION_TIMEOUT)
        if rc != 0:
            raise TdlSubprocessError(argv, rc, stdout, stderr)
        # Expected form:
        #   Version: 0.20.2\nCommit: ...\nDate: ...\ngo1.xx.x os/arch
        m = re.search(
            r"^\s*Version\s*:\s*v?(?P<v>[0-9][^\s]*)", stdout, re.MULTILINE
        )
        if not m:
            raise TdlSubprocessError(
                argv, rc, stdout, stderr,
                errors=[TdlError(kind="unknown", message="cannot parse tdl version")],
            )
        return f"v{m.group('v')}"

    # ------------------------------------------------------------------
    # Generous default — users with 1000+ dialogs need >60 s to enumerate
    # them all the first time. Override in tests if needed.
    CHAT_LS_TIMEOUT: float = 600.0

    async def chat_ls(
        self, *, output_json: Path | None = None
    ) -> list[dict]:
        """Run ``tdl chat ls -o json``; return parsed list."""
        argv = build_argv(
            self._binary,
            ["chat", "ls"],
            namespace=self._namespace,
            storage_path=self._storage_path,
            proxy=self._proxy,
            extra=["-o", "json"],
        )
        stdout, stderr, rc = await self._run_simple(argv, timeout=self.CHAT_LS_TIMEOUT)
        if rc != 0:
            raise TdlSubprocessError(argv, rc, stdout, stderr)

        data = _first_json_array(stdout)
        if output_json is not None:
            output_json.parent.mkdir(parents=True, exist_ok=True)
            output_json.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return data

    # ------------------------------------------------------------------
    async def login_exists(self) -> bool:
        """Cheap session probe — does a `chat ls` succeed without auth errors?

        Returns ``False`` if tdl reports an auth problem. Any other kind
        of failure (network, binary crash) propagates.
        """
        try:
            await self.chat_ls()
            return True
        except TdlSubprocessError as exc:
            for e in exc.errors:
                if e.kind == "auth":
                    return False
            # Detect auth errors by string match in stderr as a last resort.
            combined = f"{exc.stdout}\n{exc.stderr}".lower()
            auth_markers = (
                "auth_key_unregistered",
                "session_revoked",
                "not logged in",
                "authentication failed",
                "please login",
            )
            if any(m in combined for m in auth_markers):
                return False
            raise

    # ------------------------------------------------------------------
    async def chat_export(
        self,
        chat: str | int,
        *,
        output: Path,
        from_id: int | None = None,
        to_id: int | None = None,
        last_n: int | None = None,
        with_content: bool = True,
        include_all: bool = True,
        on_progress: ProgressCallback | None = None,
    ) -> ChatExportResult:
        """Run ``tdl chat export`` and return a :class:`ChatExportResult`.

        Progress is streamed to ``on_progress`` (or the constructor default).

        Mutually-exclusive range modes (the latter wins):
          * ``last_n`` → ``-T last -i N``: most recent N messages.
          * ``from_id`` / ``to_id`` → ``-T id -i lo,hi``: explicit ID range.

        IMPORTANT: tdl's ``-i FROM,TO`` with ``-T id`` filters by an
        INCLUSIVE range of message ids — and the smaller bound is
        ``min(FROM,TO)``, not the order you passed them. Telegram
        message ids grow monotonically over time, so to express
        "everything since message FROM" we need ``-i FROM,<some big
        number>``, NOT ``-i FROM,0`` (the old code did the latter,
        which silently fetched every historical message with id ≤
        FROM — i.e. the full backward history). We use Telegram's
        practical upper bound 2**31-1 here: ids occasionally cross
        a few hundred million on busy channels but never the int32
        ceiling.
        """
        _MAX_TG_MSG_ID = 2**31 - 1
        extra: list[str] = ["-c", str(chat), "-o", str(output)]
        if last_n is not None and last_n > 0:
            extra += ["-T", "last", "-i", str(int(last_n))]
        elif from_id is not None or to_id is not None:
            # Default ``to_id`` to the int32 ceiling so the range is
            # interpreted as "everything from FROM onwards". When the
            # caller passes an explicit ``to_id`` (e.g. a true bounded
            # range), honour it as-is. ``from_id`` defaulting to 0 is
            # fine — "everything up to TO".
            lo = from_id if from_id is not None else 0
            hi = to_id if to_id is not None else _MAX_TG_MSG_ID
            extra += ["-i", f"{lo},{hi}", "-T", "id"]
        if with_content:
            extra += ["--with-content"]
        if include_all:
            extra += ["--all"]

        argv = build_argv(
            self._binary,
            ["chat", "export"],
            namespace=self._namespace,
            storage_path=self._storage_path,
            proxy=self._proxy,
            extra=extra,
        )
        output.parent.mkdir(parents=True, exist_ok=True)

        cb = on_progress or self._on_progress
        parser = ProgressParser(default_stage="export")
        stdout, stderr, rc, errors = await self._run_streaming(argv, parser, cb)

        if rc != 0:
            raise TdlSubprocessError(argv, rc, stdout, stderr, errors=errors)

        # Parse the JSON tdl wrote to `output`.
        try:
            meta: dict[str, Any] = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise TdlSubprocessError(
                argv, rc, stdout, stderr,
                errors=[TdlError(kind="unknown", message=f"export JSON unreadable: {e}")],
            ) from e

        messages = meta.get("messages") or []
        ids = [m.get("id") for m in messages if isinstance(m, dict) and "id" in m]
        return ChatExportResult(
            chat_id=int(meta.get("id") or meta.get("chat_id") or 0),
            count_messages=len(messages),
            first_id=min(ids) if ids else None,
            last_id=max(ids) if ids else None,
            path=output,
            raw_json_meta={k: meta[k] for k in meta if k != "messages"},
        )

    # ------------------------------------------------------------------
    async def dl(
        self,
        manifest: Path,
        *,
        output_dir: Path,
        template: str | None = None,
        threads: int = 4,
        limit: int = 2,
        skip_same: bool = True,
        resume_partial: bool = True,
        reconnect_timeout: int = 10,
        on_progress: ProgressCallback | None = None,
    ) -> DlResult:
        """Run ``tdl dl`` (alias for ``download``).

        ``resume_partial=True`` passes ``--continue`` so tdl auto-resumes a
        previously-interrupted download instead of PROMPTING
        ``Found unfinished download, continue from 'N/M'?`` on stdin. We run
        tdl non-interactively, so that prompt has no stdin to read and tdl
        exits 1 with ``Incorrect function`` on Windows — which used to fail
        every retry permanently (e.g. the "Папа" chat: a partial left by an
        earlier crashed bulk export wedged every later sync). ``--continue``
        keeps the bytes already fetched; combined with ``--skip-same`` a
        resume is cheap.
        """
        extra: list[str] = [
            "-f", str(manifest),
            "-d", str(output_dir),
            "-t", str(threads),
            "-l", str(limit),
            "--reconnect-timeout", f"{reconnect_timeout}s",
        ]
        if resume_partial:
            extra += ["--continue"]
        if skip_same:
            extra += ["--skip-same"]
        if template is not None:
            extra += ["--template", template]

        argv = build_argv(
            self._binary,
            ["dl"],
            namespace=self._namespace,
            storage_path=self._storage_path,
            proxy=self._proxy,
            extra=extra,
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        cb = on_progress or self._on_progress
        parser = ProgressParser(default_stage="download")

        start = time.monotonic()
        stdout, stderr, rc, errors = await self._run_streaming(argv, parser, cb)
        elapsed = time.monotonic() - start

        if rc != 0:
            raise TdlSubprocessError(argv, rc, stdout, stderr, errors=errors)

        # tdl doesn't emit a structured summary; best-effort parse from
        # stderr. Leave zero if not found — callers have the manifest.
        files_downloaded = _count_done_markers(stderr) or _count_done_markers(stdout)
        bytes_total = _sum_bytes(stderr) or _sum_bytes(stdout)

        return DlResult(
            files_downloaded=files_downloaded,
            bytes_total=bytes_total,
            elapsed_seconds=elapsed,
            errors=errors,
        )

    # ==================================================================
    # Internals
    # ==================================================================
    async def _run_simple(
        self, argv: list[str], *, timeout: float | None
    ) -> tuple[str, str, int]:
        """Spawn, wait, return captured (stdout, stderr, rc).

        Serialised on the same bolt-DB lock as `_run_streaming` so concurrent
        chat_export / chat_ls / dl can't race on the bolt file.
        """
        async with self._db_lock:
            return await self._run_simple_unlocked(argv, timeout=timeout)

    async def _run_simple_unlocked(
        self, argv: list[str], *, timeout: float | None
    ) -> tuple[str, str, int]:
        log.debug("tdl.spawn", argv=argv, mode="simple")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return (
            (out_b or b"").decode("utf-8", errors="replace"),
            (err_b or b"").decode("utf-8", errors="replace"),
            proc.returncode if proc.returncode is not None else -1,
        )

    async def _run_streaming(
        self,
        argv: list[str],
        parser: ProgressParser,
        on_event: ProgressCallback | None,
    ) -> tuple[str, str, int, list[TdlError]]:
        """Spawn + stream stdout/stderr concurrently — serialised on the
        bolt-DB lock so concurrent calls don't trigger
        'database is used by another process'.
        """
        async with self._db_lock:
            return await self._run_streaming_unlocked(argv, parser, on_event)

    async def _run_streaming_unlocked(
        self,
        argv: list[str],
        parser: ProgressParser,
        on_event: ProgressCallback | None,
    ) -> tuple[str, str, int, list[TdlError]]:
        """Inner, lock-free variant.

        Each decoded line is fed to ``parser``; recognised events are
        dispatched to ``on_event``. Returns captured streams plus the
        list of accumulated :class:`TdlError` events.
        """
        log.debug("tdl.spawn", argv=argv, mode="streaming")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_buf: list[str] = []
        stderr_buf: list[str] = []
        errors: list[TdlError] = []

        async def pump(
            stream: asyncio.StreamReader | None, sink: list[str]
        ) -> None:
            if stream is None:
                return
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                sink.append(line)
                event = parser.feed(line)
                if event is None:
                    continue
                if isinstance(event, TdlError):
                    errors.append(event)
                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception as cb_exc:  # pragma: no cover
                        log.warning(
                            "tdl.progress.callback_error", error=str(cb_exc)
                        )

        pump_out = asyncio.create_task(pump(proc.stdout, stdout_buf))
        pump_err = asyncio.create_task(pump(proc.stderr, stderr_buf))

        try:
            rc = await proc.wait()
        except asyncio.CancelledError:
            # Upstream cancellation — kill the child cleanly.
            proc.kill()
            for t in (pump_out, pump_err):
                t.cancel()
            with _suppress(Exception):
                await proc.wait()
            raise
        finally:
            # Drain any remaining buffered lines.
            for t in (pump_out, pump_err):
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception as e:  # pragma: no cover
                    log.warning("tdl.stream.drain_error", error=str(e))

        return "\n".join(stdout_buf), "\n".join(stderr_buf), rc, errors


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
class _suppress:
    """Tiny contextlib.suppress clone used to keep `with` blocks flat."""

    def __init__(self, *exc_types: type[BaseException]) -> None:
        self._exc_types = exc_types or (Exception,)

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        return exc_type is not None and issubclass(exc_type, self._exc_types)


def _first_json_array(text: str) -> list[dict]:
    """Find the first JSON array in ``text`` and return it.

    tdl 0.20.2's `chat ls -o json` prints a plain array. Some versions
    print a surrounding summary line; be permissive.
    """
    text = text.strip()
    if text.startswith("["):
        return json.loads(text)
    # Permissive fallback: locate first '[' and last ']'.
    lo = text.find("[")
    hi = text.rfind("]")
    if lo == -1 or hi == -1 or hi < lo:
        raise ValueError("no JSON array found in tdl output")
    return json.loads(text[lo : hi + 1])


_DONE_RE = re.compile(r"\bdone\b|\bsuccess(?:ful)?\b|\bcompleted\b", re.IGNORECASE)


def _count_done_markers(text: str) -> int:
    return len(_DONE_RE.findall(text or ""))


_BYTES_RE = re.compile(
    r"(?P<n>\d+(?:\.\d+)?)\s*(?P<u>KiB|MiB|GiB|TiB|KB|MB|GB|TB|B)\b",
    re.IGNORECASE,
)

_BYTES_MULT = {
    "b": 1,
    "kb": 1_000,
    "kib": 1_024,
    "mb": 1_000_000,
    "mib": 1_048_576,
    "gb": 1_000_000_000,
    "gib": 1_073_741_824,
    "tb": 1_000_000_000_000,
    "tib": 1_099_511_627_776,
}


def _sum_bytes(text: str) -> int:
    total = 0
    for m in _BYTES_RE.finditer(text or ""):
        try:
            total += int(float(m.group("n")) * _BYTES_MULT[m.group("u").lower()])
        except (KeyError, ValueError):
            continue
    return total
