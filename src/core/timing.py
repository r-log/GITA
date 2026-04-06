"""
Reusable timing utility for performance logging.
Logs operation duration as structured key-value pairs.
"""

import time
from contextlib import asynccontextmanager

import structlog

log = structlog.get_logger()


@asynccontextmanager
async def log_timing(operation: str, **context):
    """
    Async context manager that logs the duration of an operation.

    Usage:
        async with log_timing("llm_call", agent="pr_reviewer", model="claude-sonnet"):
            result = await client.chat.completions.create(...)
    """
    started = time.time()
    try:
        yield
    finally:
        duration_ms = int((time.time() - started) * 1000)
        log.info("timing", operation=operation, duration_ms=duration_ms, **context)
