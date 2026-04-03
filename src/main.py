"""
Main entry point for the GitHub Assistant application.
FastAPI app factory — includes API routers and agent registry setup.
"""

import structlog
from fastapi import FastAPI

from src.core.logging import setup_logging
from src.api.health import router as health_router
from src.api.webhooks import router as webhooks_router
from src.api.reconcile import router as reconcile_router
from src.agents.setup import register_all_agents
from src.agents.registry import registry

# Configure structured logging before anything else
setup_logging()

# Import models so Base.metadata is populated (needed for Alembic)
import src.models  # noqa: F401

log = structlog.get_logger()

app = FastAPI(
    title="GitHub Assistant",
    description="AI-powered GitHub App that tracks repo progress, analyzes PRs, and manages milestones.",
    version="0.1.0",
)

app.include_router(health_router)
app.include_router(webhooks_router)
app.include_router(reconcile_router)


@app.on_event("startup")
async def startup():
    log.info("app_startup", msg="GitHub Assistant starting up")
    register_all_agents()
    log.info("agents_registered", agents=registry.names)
