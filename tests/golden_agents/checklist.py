"""Checklist-based evaluation for agent outputs.

A ``Checklist`` is a declarative description of what a *good* onboarding
output for a specific repo should contain. ``check_output`` runs the
checklist against an ``OnboardingResult`` and returns a list of violations
(empty list = pass).

**Why checklists instead of golden file diffs:** v1's specific failure mode
was generic phrasing, missing file:line citations, and boilerplate
milestones. Checklists target that failure mode directly (must-mention,
must-not-mention, banned titles, min_findings, require_file_line) without
being brittle on whitespace or exact wording.

Checklists live as Python modules under ``tests/golden_agents/checklists/``.
Each one exposes a module-level ``CHECKLIST`` constant. Python modules beat
YAML here because we get raw-string regex literals, type-safe field names,
and zero new dependencies.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from gita.agents.types import OnboardingResult


@dataclass
class Checklist:
    """A rubric for one repo's expected onboarding output."""

    repo_name: str
    description: str = ""

    # What the project_summary MUST contain (case-insensitive substrings or
    # regex patterns). Catches "did the agent mention the tech stack at all?"
    project_summary_must_mention: list[str] = field(default_factory=list)

    # How many findings must be produced (0 = no minimum).
    min_findings: int = 1

    # Every finding must have both a file and a line. Catches generic advice.
    require_file_line: bool = True

    # Upper bound on milestones. v1 produced too many generic ones.
    max_milestones: int = 5

    # Regex patterns that a milestone title MUST NOT match (case-insensitive).
    # v1 produced "Testing & QA", "CI/CD", "Documentation" — all banned here.
    banned_milestone_titles: list[str] = field(default_factory=list)

    # Substrings/regexes that MUST appear somewhere in the serialized output.
    # Use this to demand specific findings ("bare except", "hardcoded key").
    must_mention: list[str] = field(default_factory=list)

    # Substrings/regexes that MUST NOT appear anywhere. Catches
    # generic-advice phrases the agent shouldn't be emitting.
    must_not_mention: list[str] = field(default_factory=list)


def _serialize_for_text_search(result: OnboardingResult) -> str:
    """Flatten the OnboardingResult into a single string so regex can run
    across project_summary + findings + milestones in one pass."""
    return json.dumps(result.to_dict(), ensure_ascii=False)


def _matches(pattern: str, text: str) -> bool:
    """Case-insensitive regex search. Treats bare substrings as valid regex."""
    return bool(re.search(pattern, text, re.IGNORECASE))


def check_output(
    result: OnboardingResult, checklist: Checklist
) -> list[str]:
    """Run the checklist against ``result``. Returns a list of violations.

    An empty list means the output passes every rule.
    """
    violations: list[str] = []

    # --- project_summary ---
    for pattern in checklist.project_summary_must_mention:
        if not _matches(pattern, result.project_summary):
            violations.append(
                f"project_summary missing required mention: {pattern!r}"
            )

    # --- findings count ---
    if len(result.findings) < checklist.min_findings:
        violations.append(
            f"need at least {checklist.min_findings} findings, "
            f"got {len(result.findings)}"
        )

    # --- file:line citations on findings ---
    if checklist.require_file_line and result.findings:
        without_citation = [
            f
            for f in result.findings
            if not f.file or not f.line or f.line <= 0
        ]
        if without_citation:
            violations.append(
                f"{len(without_citation)} finding(s) missing file:line "
                f"citations (first: {without_citation[0].description[:60]!r})"
            )

    # --- milestones count ---
    if len(result.milestones) > checklist.max_milestones:
        violations.append(
            f"too many milestones: {len(result.milestones)} > "
            f"{checklist.max_milestones}"
        )

    # --- banned milestone titles ---
    for milestone in result.milestones:
        for banned in checklist.banned_milestone_titles:
            if _matches(banned, milestone.title):
                violations.append(
                    f"banned milestone title: {milestone.title!r} "
                    f"(matched pattern {banned!r})"
                )
                break  # one violation per milestone is enough

    # --- full-output must_mention / must_not_mention ---
    serialized = _serialize_for_text_search(result)
    for pattern in checklist.must_mention:
        if not _matches(pattern, serialized):
            violations.append(f"output must mention: {pattern!r}")
    for pattern in checklist.must_not_mention:
        if _matches(pattern, serialized):
            violations.append(f"output must NOT mention: {pattern!r}")

    return violations
