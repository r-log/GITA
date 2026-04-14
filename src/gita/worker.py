"""ARQ worker configuration.

Starts with::

    arq gita.worker.WorkerSettings

The worker picks up jobs enqueued by the webhook handler and runs them
sequentially (one job at a time). Startup creates shared resources
(DB engine, LLM client) that persist across jobs. Shutdown disposes them.

ARQ discovers the worker by importing ``WorkerSettings`` from this module.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from arq.connections import RedisSettings

from gita.config import settings
from gita.jobs import ALL_JOBS

logger = logging.getLogger(__name__)


def _parse_redis_url(url: str) -> RedisSettings:
    """Convert a ``redis://host:port/db`` URL to ARQ ``RedisSettings``."""
    parsed = urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip("/") or 0),
        password=parsed.password,
        username=parsed.username,
    )


async def startup(ctx: dict[str, Any]) -> None:
    """Called once when the worker starts. Creates shared resources."""
    logger.info("worker_startup redis=%s", settings.redis_url)
    # Day 4 adds: DB engine, LLM client, GitHub client.
    # For now, just mark the context as initialized.
    ctx["initialized"] = True


async def shutdown(ctx: dict[str, Any]) -> None:
    """Called once when the worker shuts down. Disposes shared resources."""
    logger.info("worker_shutdown")
    # Day 4 adds: engine.dispose(), client cleanup.


class WorkerSettings:
    """ARQ worker settings — discovered by ``arq gita.worker.WorkerSettings``."""

    functions = ALL_JOBS
    redis_settings = _parse_redis_url(settings.redis_url)

    on_startup = startup
    on_shutdown = shutdown

    # One job at a time. The dedupe layers handle duplicate deliveries.
    max_jobs = 1

    # Retry failed jobs up to 2 times with 30s delay.
    # Week 7 adds exponential backoff for LLM failures.
    max_tries = 3
    retry_delay_seconds = 30

    # Job results expire after 1 hour (for debugging via arq CLI).
    keep_result = 3600

    # Health check interval — ARQ pings Redis every 60s.
    health_check_interval = 60
