"""Health check endpoint with dependency verification."""

import structlog
from fastapi import APIRouter
from sqlalchemy import text

from src.core.config import settings
from src.core.database import async_session

log = structlog.get_logger()

router = APIRouter()


@router.get("/api/health")
async def health():
    result = {"status": "ok", "environment": settings.environment}

    # Check database connectivity
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        result["database"] = "connected"
    except Exception as e:
        log.warning("health_check_db_failed", error=str(e))
        result["database"] = f"error: {e}"
        result["status"] = "degraded"

    # Check Redis connectivity
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        await r.ping()
        await r.aclose()
        result["redis"] = "connected"
    except Exception as e:
        log.warning("health_check_redis_failed", error=str(e))
        result["redis"] = f"error: {e}"
        result["status"] = "degraded"

    return result
