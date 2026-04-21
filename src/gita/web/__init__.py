"""``gita.web`` — FastAPI application for receiving GitHub webhooks.

The app is intentionally thin: it verifies the webhook signature, filters
out bot-originated events, extracts the event type + action, and hands off
to the dispatch layer. The webhook handler enqueues jobs via an ARQ Redis
pool; the worker picks them up asynchronously.

Usage::

    uvicorn gita.web:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import FastAPI, Response

from gita.web.webhooks import router as webhooks_router
from gita.worker import _parse_redis_url

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage the ARQ Redis pool across the app's lifetime."""
    from gita.config import settings

    try:
        redis_settings = _parse_redis_url(settings.redis_url)
        pool = await create_pool(redis_settings)
        application.state.arq_pool = pool
        logger.info("arq_pool_created redis=%s", settings.redis_url)
    except Exception:
        logger.warning("arq_pool_unavailable — enqueue will return 503")
        application.state.arq_pool = None

    yield

    if getattr(application.state, "arq_pool", None) is not None:
        await application.state.arq_pool.aclose()
        logger.info("arq_pool_closed")


def create_app(*, use_lifespan: bool = True) -> FastAPI:
    """Application factory. Returns a configured FastAPI instance.

    ``use_lifespan=False`` is for tests that don't need a real Redis
    connection — they inject a fake pool via ``app.state.arq_pool``.
    """
    application = FastAPI(
        title="GITA Webhook Receiver",
        description="GitHub Assistant v2 — webhook ingestion endpoint",
        version="0.7.0",
        lifespan=lifespan if use_lifespan else None,
    )
    application.include_router(webhooks_router)

    # --- Health check endpoints ---
    @application.get("/health")
    async def health_liveness():
        """Liveness probe — always 200 if the process is running."""
        return {"status": "ok"}

    @application.get("/health/ready")
    async def health_readiness():
        """Readiness probe — checks ARQ pool availability."""
        pool = getattr(application.state, "arq_pool", None)
        if pool is None:
            return Response(
                content='{"status": "not_ready", "arq_pool": false}',
                status_code=503,
                media_type="application/json",
            )
        return {"status": "ready", "arq_pool": True}

    return application


app = create_app()
