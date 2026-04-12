"""Structured types returned by agents.

These live in their own module so the checklist infrastructure can import
them before the actual onboarding agent lands on Day 5. The dataclass fields
are meant to stay stable across Week 2 — if the agent discovers it needs
different shape, we revise here and everything else follows.
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
    severity: str         # "low" | "medium" | "high" | "critical"
    kind: str             # "bug" | "security" | "quality" | "missing" | ...
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
    verdict: str  # "approve" | "request_changes" | "comment"
    findings: list[Finding] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
