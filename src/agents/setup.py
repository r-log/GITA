"""
Agent registration — shared between FastAPI app and ARQ worker.

Models are resolved from plans/model_assignments.yml via the ModelRegistry.
Env vars (AI_MODEL_*) still work as fallback if the YAML is missing.
"""

import structlog

from src.agents.registry import registry
from src.agents.base import AgentContext
from src.agents.onboarding_agent import OnboardingAgent
from src.agents.issue_agent import IssueAnalystAgent
from src.agents.progress_agent import ProgressTrackerAgent
from src.agents.pr_agent import PRReviewAgent
from src.agents.risk_agent import RiskDetectiveAgent
from src.core.model_registry import model_registry

log = structlog.get_logger()


def _make_factory(cls, agent_name: str):
    def factory(context: AgentContext):
        model = model_registry.model_for(agent_name)
        return cls(
            installation_id=context.installation_id,
            repo_full_name=context.repo_full_name,
            repo_id=context.repo_id,
            model=model,
        )
    return factory


def register_all_agents():
    """Register all agent factories. Call from both app startup and worker startup."""
    agents = [
        ("onboarding", "Project setup specialist — scans repos, creates milestones and issues, reconciles existing state",
         OnboardingAgent),
        ("issue_analyst", "Issue quality analyst — evaluates issues with S.M.A.R.T. criteria, checks milestone alignment",
         IssueAnalystAgent),
        ("progress_tracker", "Progress analyst — tracks milestone completion %, velocity trends, blockers, deadline predictions",
         ProgressTrackerAgent),
        ("pr_reviewer", "PR reviewer — analyzes diffs for quality, checks test coverage, verifies linked issues",
         PRReviewAgent),
        ("risk_detective", "Security and risk analyst — scans for secrets, vulnerabilities, breaking changes",
         RiskDetectiveAgent),
    ]

    for name, description, cls in agents:
        registry.register_factory(name=name, description=description, factory=_make_factory(cls, name))

    log.info("agents_registered", models=model_registry.summary())
