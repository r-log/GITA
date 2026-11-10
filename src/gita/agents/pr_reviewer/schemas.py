"""Pydantic models for LLM I/O in the PR reviewer agent.

The findings call reuses ``LLMFinding`` / ``FindingsResponse`` from the
onboarding schemas — same shape, same guardrails, same conversion. Only
the summary call is PR-reviewer-specific.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Reuse the onboarding schemas for findings — same shape, same guardrails.
from gita.agents.onboarding.schemas import FindingsResponse, LLMFinding  # noqa: F401


class ReviewSummaryResponse(BaseModel):
    """LLM output for the review summary call.

    There is deliberately no ``verdict`` field: the verdict is a
    deterministic function of the verified findings (see
    ``recipe.decide_verdict``) and is given *to* the model so the
    summary agrees with it.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description=(
            "2-3 sentence review summary consistent with the given verdict. "
            "Focus on the most impactful findings. If no issues were found, "
            "say so directly."
        )
    )
    confidence: float = Field(
        default=0.0,
        description="Self-assessed confidence in the review (0.0 to 1.0)",
    )
