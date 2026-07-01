"""Proxy pool with TCP-handshake-based latency measurement and best-pick.

Persistence
-----------
A small JSON file at `settings.proxy_pool_path` holds the user-managed list
plus the latest measurements:

    {
      "version": 1,
      "entries": [
        {
          "url": "mtproto://95.214.62.224:443?secret=...",
          "label": null,
          "added_at": "2026-04-22T...",
          "last_tested_at": "2026-04-22T...",
          "last_latency_ms": 174,
          "last_status": "ok",         # "ok" | "fail" | "timeout"
          "last_error": null,
          "from_env": true
        },
        ...
      ],
      "active_url": "mtproto://95.214.62.224:443?secret=..."
    }

The store is plain JSON because proxy URLs are not high-sensitivity secrets;
they're pasted from public lists.  The vault DEK is not required, so the
store works before the user logs in.

Concurrency
-----------
A single asyncio.Lock guards file I/O. `measure_all` spawns one task per
entry with a timeout, so a dead host doesn't block the rest.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.logging_setup import get_logger
from src.telegram.proxy import ParsedProxy, parse_proxy

log = get_logger(__name__)


@dataclass
class ProxyEntry:
    url: str
    label: str | None = None
    added_at: str = ""
    last_tested_at: str | None = None
    last_latency_ms: int | None = None
    last_status: str | None = None  # "ok" | "fail" | "timeout"
    last_error: str | None = None
    from_env: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProxyEntry:
        # Tolerate missing / extra fields across versions.
        return cls(
            url=data["url"],
            label=data.get("label"),
            added_at=data.get("added_at") or _now_iso(),
            last_tested_at=data.get("last_tested_at"),
            last_latency_ms=data.get("last_latency_ms"),
            last_status=data.get("last_status"),
            last_error=data.get("last_error"),
            from_env=bool(data.get("from_env", False)),
        )


@dataclass
class PoolState:
    version: int = 1
    entries: list[ProxyEntry] = field(default_factory=list)
    active_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "entries": [e.to_dict() for e in self.entries],
            "active_url": self.active_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PoolState:
        return cls(
            version=int(data.get("version", 1)),
            entries=[ProxyEntry.from_dict(e) for e in data.get("entries", [])],
            active_url=data.get("active_url"),
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _is_mtproto(url: str) -> bool:
    """Telethon-incompatible scheme — keep these out of auto-selection."""
    if not url:
        return False
    u = url.lower()
    return (
        u.startswith("mtproto://")
        or u.startswith("mtproxy://")
        or u.startswith("tg://proxy")
        or u.startswith("https://t.me/proxy")
        or u.startswith("http://t.me/proxy")
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write atomically: temp-file + os.replace.

    On Windows the fd from `tempfile.mkstemp` MUST be closed (and not
    re-opened) before `replace`, otherwise `os.replace` raises WinError 32.
    """
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".proxy_pool_", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:  # not supported on all FSs / platforms
                pass
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


class ProxyPool:
    """Persistent + measurable proxy pool. Async-safe (single asyncio loop)."""

    def __init__(self, store_path: Path, env_seed: Iterable[str] = ()) -> None:
        self._path = store_path
        self._env_seed = list(env_seed)
        self._state: PoolState = PoolState()
        self._lock = asyncio.Lock()
        self._loaded = False

    # -------- persistence --------

    async def load(self) -> None:
        async with self._lock:
            self._state = self._read_unlocked()
            self._merge_env_unlocked()
            self._save_unlocked()
            self._loaded = True

    def _read_unlocked(self) -> PoolState:
        if not self._path.exists():
            return PoolState()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return PoolState.from_dict(data)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("proxy_pool_unreadable", path=str(self._path), error=str(exc))
            return PoolState()

    def _save_unlocked(self) -> None:
        payload = json.dumps(self._state.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
        _atomic_write(self._path, payload)

    def _merge_env_unlocked(self) -> None:
        existing = {e.url: e for e in self._state.entries}
        for url in self._env_seed:
            if not url:
                continue
            if url in existing:
                existing[url].from_env = True
                continue
            try:
                # Validate up-front so a typo doesn't poison the pool.
                parse_proxy(url)
            except ValueError as exc:
                log.warning("proxy_env_invalid", url=url, error=str(exc))
                continue
            self._state.entries.append(
                ProxyEntry(url=url, added_at=_now_iso(), from_env=True)
            )

    # -------- read API --------

    @property
    def state(self) -> PoolState:
        return self._state

    def list_entries(self) -> list[ProxyEntry]:
        return list(self._state.entries)

    def active_entry(self) -> ProxyEntry | None:
        if self._state.active_url is None:
            return None
        for e in self._state.entries:
            if e.url == self._state.active_url:
                return e
        return None

    def active_url(self) -> str | None:
        return self._state.active_url

    # -------- mutation API --------

    async def add(self, url: str, *, label: str | None = None) -> ProxyEntry:
        parse_proxy(url)  # raises ValueError on bad URL
        async with self._lock:
            for e in self._state.entries:
                if e.url == url:
                    return e
            entry = ProxyEntry(url=url, label=label, added_at=_now_iso())
            self._state.entries.append(entry)
            self._save_unlocked()
        return entry

    async def remove(self, url: str) -> bool:
        async with self._lock:
            before = len(self._state.entries)
            self._state.entries = [e for e in self._state.entries if e.url != url]
            if self._state.active_url == url:
                self._state.active_url = None
            changed = len(self._state.entries) != before
            if changed:
                self._save_unlocked()
        return changed

    async def set_active(self, url: str | None) -> None:
        async with self._lock:
            self._state.active_url = url
            self._save_unlocked()

    # -------- measurement --------

    async def measure_all(self, *, timeout: float = 4.0) -> list[ProxyEntry]:
        """TCP-handshake measure every entry concurrently. Updates state."""
        entries = self.list_entries()
        if not entries:
            return []

        async def measure_one(entry: ProxyEntry) -> None:
            try:
                p = parse_proxy(entry.url)
            except ValueError as exc:
                entry.last_status = "fail"
                entry.last_error = str(exc)
                entry.last_latency_ms = None
                entry.last_tested_at = _now_iso()
                return
            assert p is not None
            try:
                latency = await _tcp_handshake_ms(p, timeout=timeout)
                entry.last_status = "ok"
                entry.last_latency_ms = latency
                entry.last_error = None
            except TimeoutError:
                entry.last_status = "timeout"
                entry.last_latency_ms = None
                entry.last_error = f"timeout after {timeout:.1f}s"
            except OSError as exc:
                entry.last_status = "fail"
                entry.last_latency_ms = None
                entry.last_error = str(exc)
            entry.last_tested_at = _now_iso()

        await asyncio.gather(*(measure_one(e) for e in entries))
        async with self._lock:
            self._save_unlocked()
        return entries

    async def measure_and_pick_best(
        self,
        *,
        timeout: float = 4.0,
        keep_current_if_ok: bool = False,
    ) -> ProxyEntry | None:
        """Run a measurement pass and set the lowest-latency OK entry as active.

        Skips mtproto:// / mtproxy:// candidates: Telethon's MTProxy classes
        don't speak fake-TLS handshake (which all public MTProxy servers
        serve nowadays), so even when they ping fast on TCP they fail at
        the application layer. Only socks5/http candidates are considered
        for auto-selection. Manual `/proxy/select` still works for explicit
        choice.

        With `keep_current_if_ok=True` the active URL is preserved as long
        as it's still in the OK set — used by the periodic re-test so we
        don't thrash on minor latency wobbles.
        """
        entries = await self.measure_all(timeout=timeout)
        ok = [
            e for e in entries
            if e.last_status == "ok"
            and e.last_latency_ms is not None
            and not _is_mtproto(e.url)
        ]
        if not ok:
            await self.set_active(None)
            log.info("proxy_pool_no_compatible_candidates")
            return None
        ok.sort(key=lambda e: e.last_latency_ms or 10**9)
        winner = ok[0]

        current = self.active_url()
        if keep_current_if_ok and current is not None and not _is_mtproto(current):
            for e in ok:
                if e.url == current:
                    return e

        await self.set_active(winner.url)
        log.info(
            "proxy_pool_picked_best",
            url=winner.url,
            latency_ms=winner.last_latency_ms,
            candidates=len(ok),
        )
        return winner

    # -------- background re-test ticker --------

    async def run_periodic_retest(
        self,
        *,
        interval_seconds: int,
        timeout: float = 4.0,
        keep_current_if_ok: bool = True,
    ) -> None:
        """Long-running task: re-measure every `interval_seconds`.

        Designed to run as an `asyncio.create_task(...)` in app lifespan.
        Cancelling the task cleanly aborts the loop.
        """
        if interval_seconds <= 0 or not self.list_entries():
            return
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    await self.measure_and_pick_best(
                        timeout=timeout,
                        keep_current_if_ok=keep_current_if_ok,
                    )
                except Exception as exc:  # noqa: BLE001 — never let the loop die
                    log.warning("proxy_periodic_retest_failed", error=str(exc))
        except asyncio.CancelledError:
            log.info("proxy_periodic_retest_stopped")
            raise


async def _tcp_handshake_ms(p: ParsedProxy, *, timeout: float) -> int:
    """Open a TCP connection to (host, port). Returns ms or raises."""
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    fut = loop.create_connection(
        lambda: asyncio.Protocol(), host=p.host, port=p.port,
    )
    transport, _ = await asyncio.wait_for(fut, timeout=timeout)
    try:
        latency = int((loop.time() - t0) * 1000)
    finally:
        transport.close()
    return latency


# -------- pure helper for `socket`-based testing under a sync context --------


def sync_tcp_handshake_ms(host: str, port: int, *, timeout: float = 4.0) -> int:
    """Synchronous variant — used by tests that want to validate behaviour
    without an event loop."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.connect((host, port))
        return int((time.time() - t0) * 1000)
    finally:
        s.close()
