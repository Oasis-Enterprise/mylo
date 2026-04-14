"""Structured logging via structlog.

JSON output in production so HA's log viewer can parse it; colorized console
output in dev (detected via TTY). Log level controlled by the ``MYLO_LOG_LEVEL``
env var, defaulting to INFO.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog


def configure_logging() -> None:
    level_name = os.environ.get("MYLO_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    # Silence noisy third-party INFO logs — they interleave with our own
    # stdout writes in the chat REPL and occasionally mangle output.
    # Users can still re-enable per-logger via MYLO_LOG_LEVEL=DEBUG.
    for noisy in ("httpx", "httpcore", "anthropic", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(
            logging.DEBUG if level <= logging.DEBUG else logging.WARNING
        )

    is_tty = sys.stdout.isatty()

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if is_tty:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
