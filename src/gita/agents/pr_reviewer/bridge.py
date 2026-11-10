"""Decision bridge — turn a PRReviewResult into a comment Decision.

Posts the review as a single comment on the PR. Week 5 can upgrade to
inline PR review comments (GitHub's Review API) once we build the
diff-line-to-file-line mapping.
"""
from __future__ import annotations

from gita.agents.decisions import Decision
from gita.agents.render import inline, quote_lines
from gita.agents.types import Finding, PRReviewResult

# Collapse findings into <details> when there are this many or more.
_COLLAPSE_THRESHOLD = 6

_SEVERITY_BADGE: dict[str, str] = {
    "critical": "\U0001f534",  # red circle
    "high": "\U0001f7e0",      # orange circle
    "medium": "\U0001f7e1",    # yellow circle
    "low": "\U0001f535",       # blue circle
}

_VERDICT_DISPLAY: dict[str, tuple[str, str]] = {
    "approve": ("\u2705", "Approved"),              # green check
    "comment": ("\U0001f4ac", "Comment"),            # speech bubble
    "request_changes": ("\u26a0\ufe0f", "Changes Requested"),  # warning
}


def _severity_icon(severity: str) -> str:
    return _SEVERITY_BADGE.get(severity.lower(), "\u26aa")  # white circle fallback


def _render_finding_card(finding: Finding) -> list[str]:
    """Render a single finding as a blockquote card."""
    icon = _severity_icon(finding.severity)
    lines = [
        f"> {icon} **{finding.severity.upper()}** \u2014 "
        f"_{finding.kind}_ at `{inline(finding.file)}:{finding.line}`",
        ">",
        *quote_lines(finding.description),
    ]
    if finding.fix_sketch:
        lines.append(">")
        lines.extend(quote_lines(finding.fix_sketch, prefix="\U0001f4a1 "))
    lines.append("")
    return lines


def _render_coverage_notes(result: PRReviewResult) -> list[str]:
    """Say what the review did *not* cover, so silence isn't read as approval."""
    notes: list[str] = []
    if result.unreviewed_files:
        shown = ", ".join(f"`{inline(p)}`" for p in result.unreviewed_files[:10])
        more = len(result.unreviewed_files) - 10
        suffix = f" and {more} more" if more > 0 else ""
        notes.append(
            f"\u26a0\ufe0f Not reviewed ({len(result.unreviewed_files)} "
            f"file{'s' if len(result.unreviewed_files) != 1 else ''}): "
            f"{shown}{suffix}"
        )
    if result.dropped_findings:
        notes.append(
            f"\u2139\ufe0f {result.dropped_findings} finding"
            f"{'s' if result.dropped_findings != 1 else ''} discarded by "
            "automated verification."
        )
    if notes:
        notes.append("")
    return notes


def _render_review_body(result: PRReviewResult) -> str:
    """Render the review as a markdown comment body."""
    # --- Header ---
    verdict_icon, verdict_text = _VERDICT_DISPLAY.get(
        result.verdict, ("\u2753", result.verdict)
    )
    lines: list[str] = [
        f"## \U0001f50d PR Review \u2014 #{result.pr_number}",
        "",
        *quote_lines(result.summary),
        "",
        f"{verdict_icon} **{verdict_text}** "
        f"\u00b7 {len(result.findings)} finding{'s' if len(result.findings) != 1 else ''} "
        f"\u00b7 confidence {result.confidence:.0%}",
        "",
    ]

    lines.extend(_render_coverage_notes(result))

    if not result.findings:
        lines.append("\u2705 No issues found in the reviewed changes.")
        lines.append("")
    else:
        # --- Group findings by file ---
        by_file: dict[str, list[Finding]] = {}
        for finding in result.findings:
            by_file.setdefault(finding.file, []).append(finding)

        collapse = len(result.findings) >= _COLLAPSE_THRESHOLD

        findings_block: list[str] = []
        for file_path, file_findings in by_file.items():
            findings_block.append("---")
            findings_block.append("")
            findings_block.append(f"### \U0001f4c4 `{file_path}`")
            findings_block.append("")
            for finding in file_findings:
                findings_block.extend(_render_finding_card(finding))

        if collapse:
            lines.append("<details>")
            lines.append(
                f"<summary>\U0001f50e {len(result.findings)} findings "
                f"\u2014 click to expand</summary>"
            )
            lines.append("")
            lines.extend(findings_block)
            lines.append("")
            lines.append("</details>")
        else:
            lines.extend(findings_block)

    # --- Footer ---
    lines.append("")
    lines.append("---")
    lines.append(
        f"<sub>\U0001f916 GITA v0.1.0 \u00b7 PR reviewer \u00b7 "
        f"confidence {result.confidence:.0%}</sub>"
    )
    return "\n".join(lines)


def build_pr_review_decision(
    result: PRReviewResult,
    repo_full_name: str,
    pr_number: int,
) -> Decision:
    """Wrap a PRReviewResult as a ``comment`` Decision on the PR.

    Uses the existing ``comment`` action dispatch — the PR number goes
    into ``target.issue`` because GitHub treats PR comments and issue
    comments as the same endpoint (``POST /issues/{n}/comments``).
    """
    evidence = [
        f"pr_reviewer verdict: {result.verdict}",
        f"{len(result.findings)} findings produced",
        f"confidence: {result.confidence:.2f}",
    ]
    for finding in result.findings[:3]:
        evidence.append(
            f"{finding.severity} {finding.kind} at "
            f"{finding.file}:{finding.line}"
        )

    return Decision(
        action="comment",
        target={"repo": repo_full_name, "issue": pr_number},
        payload={"body": _render_review_body(result)},
        evidence=evidence,
        confidence=result.confidence,
    )
