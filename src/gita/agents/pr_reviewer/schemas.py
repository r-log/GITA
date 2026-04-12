"""Pydantic models for LLM I/O in the PR reviewer agent.

The findings call reuses ``LLMFinding`` / ``FindingsResponse`` from the
onboarding schemas — same shape, same guardrails, same conversion. Only
the summary call is PR-reviewer-specific (it adds a verdict field).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# Reuse the onboarding schemas for findings — same shape, same guardrails.
from gita.agents.onboarding.schemas import FindingsResponse, LLMFinding  # noqa: F401


class ReviewSummaryResponse(BaseModel):
    """LLM output for the review summary call."""

    summary: str = Field(
        description=(
            "2-3 sentence review summary. Focus on the most impactful "
            "findings. If no issues were found, say so directly."
        )
    )
    verdict: str = Field(
        description=(
            "One of: 'approve' (no issues), 'comment' (minor issues, "
            "informational), 'request_changes' (bugs or security issues "
            "that should be fixed before merge)"
        )
    )
    confidence: float = Field(
        default=0.0,
        description="Self-assessed confidence in the review (0.0 to 1.0)",
    )
