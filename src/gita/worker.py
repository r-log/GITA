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


def _mask_url(url: str) -> str:
    """Mask password in a URL for safe logging."""
    parsed = urlparse(url)
    if parsed.password:
        masked = parsed._replace(
            netloc=f"{parsed.username or ''}:***@{parsed.hostname}:{parsed.port}"
        )
        return masked.geturl()
    return url


async def startup(ctx: dict[str, Any]) -> None:
    """Called once when the worker starts.

    Creates a shared DB engine, validates connectivity with a ``SELECT 1``,
    and logs key configuration. Jobs use their own per-call sessions
    (via ``SessionLocal``), but the startup check catches config errors
    early — before the first job arrives.
    """
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
        logger.warning(
            "worker_db_check failed — jobs will fail individually"
        )
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
