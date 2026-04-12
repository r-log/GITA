"""Onboarding agent — split into three submodules.

- ``schemas``  — pydantic LLM I/O models
- ``recipe``   — the three-stage pipeline + ``run_onboarding``
- ``bridge``   — Decision bridges (comment + issue) + body renderers

All public names are re-exported here so that ``from gita.agents.onboarding
import run_onboarding`` continues to work everywhere.
"""
from gita.agents.onboarding.bridge import (
    _COLLAPSE_THRESHOLD,
    _render_comment_body,
    _render_issue_body,
    build_onboarding_comment_decision,
    build_onboarding_issue_decisions,
)
from gita.agents.onboarding.recipe import OnboardingError, run_onboarding
from gita.agents.onboarding.schemas import (
    FindingsResponse,
    LLMFinding,
    LLMMilestone,
    MilestonesResponse,
    PickFilesResponse,
)

__all__ = [
    # recipe
    "OnboardingError",
    "run_onboarding",
    # schemas
    "PickFilesResponse",
    "FindingsResponse",
    "MilestonesResponse",
    "LLMFinding",
    "LLMMilestone",
    # bridge
    "build_onboarding_comment_decision",
    "build_onboarding_issue_decisions",
    "_render_comment_body",
    "_render_issue_body",
    "_COLLAPSE_THRESHOLD",
]
