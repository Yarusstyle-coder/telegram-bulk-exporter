"""Exponential-backoff retry wrapper for tdl subprocess calls.

Flood-wait errors are **not** retried here — the caller (orchestrator)
must surface the countdown in the UI and schedule a delayed resume.
Network / transient auth errors get a bounded retry with jitter.

The wrapper inspects either:

* a raised :class:`src.services.tdl_wrapper.TdlSubprocessError` (which
  carries a list of structured :class:`TdlError` events), or
* a generic ``Exception`` (treated as retriable by default up to
  ``max_attempts``).

If any structured error in a :class:`TdlSubprocessError` is of kind
``flood_wait``, the function raises :class:`FloodWaitNotRetried` — the
orchestrator must handle that case.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from src.logging_setup import get_logger
from src.services.tdl_types import TdlError

log = get_logger(__name__)


class FloodWaitNotRetried(Exception):
    """Raised when a flood-wait error surfaces; caller handles the wait."""

    def __init__(self, wait_seconds: int, message: str) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds


def _classify(exc: BaseException) -> tuple[str, int | None, str]:
    """Return (kind, wait_seconds, message) extracted from ``exc``.

    ``kind`` is one of: ``'flood_wait'``, ``'auth'``, ``'network'``,
    ``'unknown'``. For anything we can't classify we return
    ``('unknown', None, str(exc))``.
    """
    # Late import to avoid a circular dep (wrapper imports from retry? no —
    # but keep the isolation cheap anyway).
    try:
        from src.services.tdl_wrapper import TdlSubprocessError
    except Exception:  # pragma: no cover
        TdlSubprocessError = ()  # type: ignore[assignment]

    errs: list[TdlError] = []
    if isinstance(exc, TdlSubprocessError):  # type: ignore[arg-type]
        errs = list(getattr(exc, "errors", []) or [])

    for e in errs:
        if e.kind == "flood_wait":
            return ("flood_wait", e.wait_seconds, e.message)
    for e in errs:
        if e.kind in {"network", "auth"}:
            return (e.kind, None, e.message)
    if errs:
        return (errs[-1].kind, errs[-1].wait_seconds, errs[-1].message)
    return ("unknown", None, str(exc))


async def with_retry[T](
    coro_factory: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
) -> T:
    """Run ``coro_factory()`` with exponential backoff.

    ``coro_factory`` is a zero-arg callable that produces a fresh
    awaitable each attempt — mandatory because coroutines are
    single-shot.

    Retries on network / transient errors. If the underlying call fails
    with a flood-wait (inspected via
    :class:`src.services.tdl_wrapper.TdlSubprocessError`), this function
    re-raises a :class:`FloodWaitNotRetried` so the orchestrator can
    schedule the resume.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except FloodWaitNotRetried:
            raise
        except BaseException as exc:
            kind, wait_seconds, message = _classify(exc)

            if kind == "flood_wait":
                raise FloodWaitNotRetried(
                    wait_seconds=wait_seconds or 0, message=message
                ) from exc

            if attempt == max_attempts:
                raise

            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            jitter = random.uniform(0, 0.5)
            total = delay + jitter
            log.warning(
                "tdl.retry.backoff",
                attempt=attempt,
                max_attempts=max_attempts,
                delay_seconds=round(total, 3),
                kind=kind,
                error=message,
            )
            await asyncio.sleep(total)

    # Unreachable — loop either returns or raises.
    raise RuntimeError("with_retry exhausted without return")  # pragma: no cover
