"""Decision bridges — turn an OnboardingResult into Decision objects.

Two bridges:

1. ``build_onboarding_comment_decision`` — wraps the entire result as one
   ``comment`` Decision. Used by the ``--post-to`` flow.
2. ``build_onboarding_issue_decisions`` — wraps each milestone as a
   separate ``create_issue`` Decision. Used by the ``--create-issues`` flow.

Both are pure functions: they take an ``OnboardingResult`` and return
``Decision`` objects without any I/O.
"""
from __future__ import annotations

import logging
from typing import Any

from gita.agents.decisions import Decision
from gita.agents.types import Finding, Milestone, OnboardingResult

logger = logging.getLogger(__name__)

_COLLAPSE_THRESHOLD = 6

_SEVERITY_BADGE: dict[str, str] = {
    "critical": "\U0001f534",  # red circle
    "high": "\U0001f7e0",      # orange circle
    "medium": "\U0001f7e1",    # yellow circle
    "low": "\U0001f535",       # blue circle
}


def _severity_icon(severity: str) -> str:
    return _SEVERITY_BADGE.get(severity.lower(), "\u26aa")


def _wrap_details(
    summary: str, body_lines: list[str], *, collapse: bool
) -> list[str]:
    if not collapse:
        return body_lines
    wrapped = ["<details>", f"<summary>{summary}</summary>", ""]
    wrapped.extend(body_lines)
    wrapped.append("</details>")
    return wrapped


# ---------------------------------------------------------------------------
# Finding card (shared between comment + issue renderers)
# ---------------------------------------------------------------------------
def _render_finding_card(index: int, finding: Finding) -> list[str]:
    icon = _severity_icon(finding.severity)
    lines = [
        f"> {icon} **{index + 1}. {finding.severity.upper()}** \u2014 "
        f"_{finding.kind}_ at `{finding.file}:{finding.line}`",
        ">",
        f"> {finding.description}",
    ]
    if finding.fix_sketch:
        lines.append(">")
        lines.append(f"> \U0001f4a1 {finding.fix_sketch}")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Milestone card
# ---------------------------------------------------------------------------
def _render_milestone_card(
    milestone: Milestone, findings: list[Finding]
) -> list[str]:
    conf_pct = f"{milestone.confidence:.0%}"
    lines = [
        f"#### \U0001f3af {milestone.title}",
        "",
        f"> {milestone.summary}",
        "",
    ]
    if milestone.finding_indices:
        cited = [
            findings[i]
            for i in milestone.finding_indices
            if 0 <= i < len(findings)
        ]
        if cited:
            for finding in cited:
                icon = _severity_icon(finding.severity)
                lines.append(
                    f"- {icon} `{finding.file}:{finding.line}` \u2014 "
                    f"{finding.description[:80]}"
                    f"{'...' if len(finding.description) > 80 else ''}"
                )
            lines.append("")
    lines.append(f"_Confidence: {conf_pct}_")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Comment bridge — full onboarding review as one comment
# ---------------------------------------------------------------------------
def _render_comment_body(result: OnboardingResult) -> str:
    lines: list[str] = [
        f"## \U0001f4cb Onboarding Review \u2014 `{result.repo_name}`",
        "",
        f"> {result.project_summary}",
        "",
        f"\U0001f4ca {len(result.findings)} finding"
        f"{'s' if len(result.findings) != 1 else ''}"
        f" \u00b7 {len(result.milestones)} milestone"
        f"{'s' if len(result.milestones) != 1 else ''}"
        f" \u00b7 confidence {result.confidence:.0%}",
        "",
    ]

    # --- Findings ---
    if result.findings:
        collapse = len(result.findings) >= _COLLAPSE_THRESHOLD
        findings_block: list[str] = [
            "---",
            "",
            f"### \U0001f50e Findings ({len(result.findings)})",
            "",
        ]
        for i, finding in enumerate(result.findings):
            findings_block.extend(_render_finding_card(i, finding))

        if collapse:
            lines.extend(
                _wrap_details(
                    f"\U0001f50e Findings ({len(result.findings)}) "
                    f"\u2014 click to expand",
                    findings_block,
                    collapse=True,
                )
            )
            lines.append("")
        else:
            lines.extend(findings_block)
    else:
        lines.append(
            "\u2705 No concrete findings \u2014 "
            "the reviewed files looked clean."
        )
        lines.append("")

    # --- Milestones ---
    if result.milestones:
        collapse = len(result.milestones) >= _COLLAPSE_THRESHOLD
        milestones_block: list[str] = [
            "---",
            "",
            f"### \U0001f5fa\ufe0f Proposed Milestones "
            f"({len(result.milestones)})",
            "",
        ]
        for milestone in result.milestones:
            milestones_block.extend(
                _render_milestone_card(milestone, result.findings)
            )

        if collapse:
            lines.extend(
                _wrap_details(
                    f"\U0001f5fa\ufe0f Milestones "
                    f"({len(result.milestones)}) \u2014 click to expand",
                    milestones_block,
                    collapse=True,
                )
            )
            lines.append("")
        else:
            lines.extend(milestones_block)

    # --- Footer ---
    lines.append("")
    lines.append("---")
    lines.append(
        f"<sub>\U0001f916 GITA v0.1.0 \u00b7 onboarding agent \u00b7 "
        f"confidence {result.confidence:.0%}</sub>"
    )
    return "\n".join(lines)


def build_onboarding_comment_decision(
    result: OnboardingResult,
    repo_full_name: str,
    issue_number: int,
) -> Decision:
    """Wrap an OnboardingResult as a ``comment`` Decision."""
    evidence = [
        f"{len(result.findings)} concrete findings produced",
        f"{len(result.milestones)} proposed milestones",
        f"agent overall confidence: {result.confidence:.2f}",
    ]
    if result.findings:
        evidence.append(
            f"strongest finding: {result.findings[0].severity} "
            f"{result.findings[0].kind} at "
            f"{result.findings[0].file}:{result.findings[0].line}"
        )

    return Decision(
        action="comment",
        target={"repo": repo_full_name, "issue": issue_number},
        payload={"body": _render_comment_body(result)},
        evidence=evidence,
        confidence=result.confidence,
    )


# ---------------------------------------------------------------------------
# Issue bridge — one create_issue Decision per milestone
# ---------------------------------------------------------------------------
def _render_issue_body(
    milestone: Milestone,
    findings: list[Finding],
    source_repo: str,
) -> str:
    """Render an issue body for a single milestone."""
    conf_pct = f"{milestone.confidence:.0%}"
    lines: list[str] = [
        f"> {milestone.summary.strip()}",
        "",
        f"_Confidence: {conf_pct}_",
        "",
    ]

    cited = [
        findings[i]
        for i in milestone.finding_indices
        if 0 <= i < len(findings)
    ]
    if cited:
        collapse = len(cited) >= _COLLAPSE_THRESHOLD
        checklist: list[str] = [
            f"## \U0001f50e Findings ({len(cited)})",
            "",
        ]
        for finding in cited:
            icon = _severity_icon(finding.severity)
            checklist.append(
                f"- [ ] {icon} **{finding.severity.upper()}** "
                f"_{finding.kind}_ at `{finding.file}:{finding.line}`"
            )
            checklist.append(f"  {finding.description}")
            if finding.fix_sketch:
                checklist.append(f"  \U0001f4a1 {finding.fix_sketch}")
            checklist.append("")

        if collapse:
            lines.extend(
                _wrap_details(
                    f"\U0001f50e Findings ({len(cited)}) "
                    f"\u2014 click to expand",
                    checklist,
                    collapse=True,
                )
            )
        else:
            lines.extend(checklist)
        lines.append("")

    lines.append("---")
    lines.append(
        f"<sub>\U0001f916 GITA v0.1.0 \u00b7 onboarding agent \u00b7 "
        f"source: `{source_repo}`</sub>"
    )
    return "\n".join(lines)


def build_onboarding_issue_decisions(
    result: OnboardingResult,
    target_repo: str,
    *,
    fallback_comment_target: int | None = None,
    default_labels: list[str] | None = None,
) -> list[Decision]:
    """Wrap an OnboardingResult as a list of ``create_issue`` Decisions."""
    if not result.milestones:
        return []

    decisions: list[Decision] = []
    for milestone in result.milestones:
        cited_indices = [
            i for i in milestone.finding_indices if 0 <= i < len(result.findings)
        ]
        if not cited_indices:
            logger.warning(
                "skipping_milestone_no_valid_findings title=%s",
                milestone.title,
            )
            continue

        evidence = [
            f"milestone: {milestone.title}",
            f"agent overall confidence: {result.confidence:.2f}",
            f"source repo: {result.repo_name}",
        ]
        for idx in cited_indices:
            finding = result.findings[idx]
            evidence.append(
                f"{finding.severity} {finding.kind} at "
                f"{finding.file}:{finding.line}"
            )

        target: dict[str, Any] = {"repo": target_repo}
        if fallback_comment_target is not None:
            target["fallback_issue"] = fallback_comment_target

        sig_keys = sorted(
            f"{result.findings[i].file}:{result.findings[i].line}"
            for i in cited_indices
        )

        payload: dict[str, Any] = {
            "title": milestone.title,
            "body": _render_issue_body(
                milestone, result.findings, result.repo_name
            ),
            "_signature_keys": sig_keys,
        }
        if default_labels:
            payload["labels"] = list(default_labels)

        decisions.append(
            Decision(
                action="create_issue",
                target=target,
                payload=payload,
                evidence=evidence,
                confidence=milestone.confidence,
            )
        )

    return decisions
