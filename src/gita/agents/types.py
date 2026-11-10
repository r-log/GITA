"""Structured types returned by agents.

They live in their own module so the bridges, guardrails and golden-test
checklists can share them without importing the agents themselves.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Finding:
    """One concrete thing an agent found in a file. Always has a file:line
    citation — findings without one are rejected by the checklist."""

    file: str
    line: int
    severity: str  # "low" | "medium" | "high" | "critical"
    kind: str  # "bug" | "security" | "quality" | "missing" | ...
    description: str
    fix_sketch: str = ""


@dataclass
class Milestone:
    """A group of related findings with a human-readable title."""

    title: str
    summary: str
    finding_indices: list[int] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class OnboardingResult:
    """The structured output of ``run_onboarding``."""

    repo_name: str
    project_summary: str
    findings: list[Finding] = field(default_factory=list)
    milestones: list[Milestone] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PRReviewResult:
    """The structured output of ``run_pr_review``."""

    repo_name: str
    pr_number: int
    pr_title: str
    summary: str  # 2-3 sentence review from the LLM
    # Decided in code from the verified findings, never by the model:
    # "approve" | "request_changes" | "comment".
    verdict: str
    findings: list[Finding] = field(default_factory=list)
    confidence: float = 0.0
    # Findings the guardrails discarded. Reported so a reader knows the
    # review isn't silently narrower than what the model saw.
    dropped_findings: int = 0
    # Changed files the model was not shown in full (binary, patch too
    # large, over the file cap or prompt budget).
    unreviewed_files: list[str] = field(default_factory=list)
    # Which model reviewed the change, at which tier, and why (agents/routing.py).
    model: str | None = None
    tier: str | None = None
    route_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
