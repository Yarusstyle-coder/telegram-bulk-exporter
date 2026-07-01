"""Structured logging via structlog + stdlib logging.

On Windows, the default console encoding is cp1251 — printing unicode
(Cyrillic in error messages, exotic characters in tracebacks, etc.)
raises UnicodeEncodeError mid-handler and turns benign exceptions into
500s. We force stdout to UTF-8 with `errors='backslashreplace'` so
logging never breaks the request that's trying to log.
"""

from __future__ import annotations

import logging
import sys

import structlog


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — already configured / not a TextIOWrapper
            pass


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib logging for the whole app."""
    _force_utf8_stdout()

    numeric = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric,
        force=True,  # let us re-call this in tests / on uvicorn reload
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    return structlog.get_logger(name)
