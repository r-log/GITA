"""ARQ worker configuration.

Starts with::

    arq gita.worker.WorkerSettings

The worker picks up jobs enqueued by the webhook handler and runs them
sequentially (one job at a time). Startup validates DB connectivity and
stores the engine for shared use. Shutdown disposes the engine.

ARQ discovers the worker by importing ``WorkerSettings`` from this module.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from arq.connections import RedisSettings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from gita.config import settings
from gita.jobs import ALL_JOBS
from gita.logging_config import configure_logging

logger = logging.getLogger(__name__)


def _parse_redis_url(url: str) -> RedisSettings:
    """``redis://`` / ``rediss://`` URL to ARQ ``RedisSettings``.

    Delegates to ARQ so ``rediss://`` enables TLS; hand-parsing the URL
    used to drop the scheme and connect in plaintext.
    """
    return RedisSettings.from_dsn(url)


def _mask_url(url: str) -> str:
    """Mask password in a URL for safe logging."""
    parsed = urlparse(url)
    if parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        masked = parsed._replace(netloc=f"{parsed.username or ''}:***@{host}{port}")
        return masked.geturl()
    return url


async def startup(ctx: dict[str, Any]) -> None:
    """Called once when the worker starts.

    Creates a shared DB engine, validates connectivity with a ``SELECT 1``,
    and logs key configuration. Jobs use their own per-call sessions
    (via ``SessionLocal``), but the startup check catches config errors
    early — before the first job arrives.
    """
    configure_logging(settings.log_level)
    logger.info(
        "worker_startup redis=%s db=%s write_mode=%s",
        _mask_url(settings.redis_url),
        _mask_url(settings.database_url),
        settings.write_mode,
    )

    engine = create_async_engine(settings.database_url, echo=False)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("worker_db_check ok")
    except Exception:
        logger.warning("worker_db_check failed — jobs will fail individually")
    ctx["engine"] = engine
    ctx["initialized"] = True


async def shutdown(ctx: dict[str, Any]) -> None:
    """Called once when the worker shuts down. Disposes the shared engine."""
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()
        logger.info("worker_engine_disposed")
    logger.info("worker_shutdown")


class WorkerSettings:
    """ARQ worker settings — discovered by ``arq gita.worker.WorkerSettings``."""

    functions = ALL_JOBS
    redis_settings = _parse_redis_url(settings.redis_url)

    on_startup = startup
    on_shutdown = shutdown

    # One job at a time: the reindex job owns the repo checkout.
    max_jobs = 1

    # Jobs raise arq.Retry for transient failures (see gita.jobs); ARQ
    # does not retry ordinary exceptions, so this only bounds those.
    max_tries = 3

    # A review is two LLM calls (up to 180 s each, plus client retries)
    # and a handful of GitHub calls; ARQ's 300 s default was too short.
    job_timeout = 900

    # Job IDs only collapse bursts of identical deliveries; durable
    # idempotency lives in agent_actions. A long keep_result would make
    # ARQ refuse legitimate re-enqueues of the same ID (e.g. a retry
    # after a failed job), so results are kept just long enough to debug.
    keep_result = 300

    # Health check interval — ARQ pings Redis every 60s.
    health_check_interval = 60
