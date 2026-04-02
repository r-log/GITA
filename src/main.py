"""
Main entry point for the GitHub Assistant application.
FastAPI app factory — includes API routers and agent registry setup.
"""

import structlog
from fastapi import FastAPI

from src.core.logging import setup_logging
from src.api.health import router as health_router
from src.api.webhooks import router as webhooks_router

# Configure structured logging before anything else
setup_logging()
from src.agents.registry import registry
from src.agents.base import AgentContext
from src.agents.onboarding_agent import OnboardingAgent
from src.agents.issue_agent import IssueAnalystAgent
from src.agents.progress_agent import ProgressTrackerAgent
from src.agents.pr_agent import PRReviewAgent
from src.agents.risk_agent import RiskDetectiveAgent

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


def _make_factory(cls):
    """Generic factory that passes installation_id, repo_full_name, and repo_id from context."""
    def factory(context: AgentContext):
        return cls(
            installation_id=context.installation_id,
            repo_full_name=context.repo_full_name,
            repo_id=context.repo_id,
        )
    return factory


@app.on_event("startup")
async def startup():
    log.info("app_startup", msg="GitHub Assistant starting up")

    agents = [
        ("onboarding", "Project setup specialist — scans repos, creates milestones and issues, reconciles existing state", OnboardingAgent),
        ("issue_analyst", "Issue quality analyst — evaluates issues with S.M.A.R.T. criteria, checks milestone alignment", IssueAnalystAgent),
        ("progress_tracker", "Progress analyst — tracks milestone completion %, velocity trends, blockers, deadline predictions", ProgressTrackerAgent),
        ("pr_reviewer", "PR reviewer — analyzes diffs for quality, checks test coverage, verifies linked issues", PRReviewAgent),
        ("risk_detective", "Security and risk analyst — scans for secrets, vulnerabilities, breaking changes", RiskDetectiveAgent),
    ]

    for name, description, cls in agents:
        registry.register_factory(name=name, description=description, factory=_make_factory(cls))

    log.info("agents_registered", agents=registry.names)
