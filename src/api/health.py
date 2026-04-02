"""Health check endpoint."""

from fastapi import APIRouter

from src.core.config import settings

router = APIRouter()


@router.get("/api/health")
async def health():
    return {"status": "ok", "environment": settings.environment}
