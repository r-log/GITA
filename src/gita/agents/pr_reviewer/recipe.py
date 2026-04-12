"""Two-stage PR review recipe.

Pipeline:
    1. diff_context_view(session, repo, hunks)           deterministic
    2. LLM: "given the diff + context, find issues"       call 1
    3. verify_findings(session, repo_id, findings)         deterministic (guardrails)
    4. LLM: "summarize verified findings into a review"    call 2
    5. structural_confidence blending                       deterministic

No "pick which files to read" step — the PR diff already tells us what
changed. No "group into milestones" step — the review is a single
comment, not a set of issues.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.guardrails import structural_confidence, verify_findings
from gita.agents.onboarding.schemas import FindingsResponse
from gita.agents.pr_reviewer.diff_parser import DiffHunk
from gita.agents.pr_reviewer.schemas import ReviewSummaryResponse
from gita.agents.types import Finding, PRReviewResult
from gita.github.client import PRInfo
from gita.llm.client import LLMClient
from gita.views._common import resolve_repo
from gita.views.diff_context import FileContext, diff_context_view

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_REVIEW_MAX_TOKENS = 4096
_SUMMARY_MAX_TOKENS = 1024
_MAX_FILES_PER_REVIEW = 20
_FILE_CONTEXT_CAP_CHARS = 8_000


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------
def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------
def _render_file_for_prompt(ctx: FileContext) -> str:
    """Render one changed file's diff + context for the LLM prompt."""
    hunk = ctx.diff_hunk
    lines: list[str] = [
        f"=== {hunk.file_path} ({hunk.status}, "
        f"+{hunk.additions}/-{hunk.deletions}) ===",
    ]

    # The raw diff patch (the actual changes).
    if hunk.patch:
        lines.append("")
        lines.append("DIFF:")
        lines.append(hunk.patch)

    # Symbols near the changed lines.
    if ctx.symbols_near_changes:
        lines.append("")
        lines.append("SYMBOLS NEAR CHANGES:")
        for sym in ctx.symbols_near_changes:
            parent = f" in {sym.parent_class}" if sym.parent_class else ""
            lines.append(
                f"  {sym.kind} {sym.name}{parent} "
                f"(lines {sym.start_line}-{sym.end_line})"
            )

    # Reverse dependencies — impact signal.
    if ctx.imported_by:
        lines.append("")
        lines.append(
            f"IMPORTED BY ({len(ctx.imported_by)} files): "
            + ", ".join(ctx.imported_by[:5])
        )
        if len(ctx.imported_by) > 5:
            lines.append(f"  ... and {len(ctx.imported_by) - 5} more")

    # Surrounding code context from the index (capped).
    if ctx.content:
        content = ctx.content
        if len(content) > _FILE_CONTEXT_CAP_CHARS:
            content = content[:_FILE_CONTEXT_CAP_CHARS]
            lines.append("")
            lines.append(
                f"FILE CONTEXT (first {_FILE_CONTEXT_CAP_CHARS} chars, "
                f"{ctx.line_count} lines total):"
            )
        else:
            lines.append("")
            lines.append(f"FILE CONTEXT ({ctx.line_count} lines):")
        lines.append(_prepend_line_numbers(content))

    lines.append("")
    return "\n".join(lines)


def _prepend_line_numbers(content: str) -> str:
    out = []
    for i, line in enumerate(content.splitlines(), start=1):
        out.append(f"{i:>4}: {line}")
    return "\n".join(out)


def _render_findings_for_summary(
    findings: list[Finding], pr_title: str, total_files: int
) -> str:
    lines = [
        f"PR: {pr_title}",
        f"Files changed: {total_files}",
        "",
        f"Verified findings ({len(findings)}):",
        "",
    ]
    if not findings:
        lines.append("(none — the changed code looks clean)")
    else:
        for i, f in enumerate(findings):
            lines.append(
                f"[{i}] {f.severity:<8} {f.kind:<10} {f.file}:{f.line}"
            )
            lines.append(f"    {f.description}")
            if f.fix_sketch:
                lines.append(f"    fix: {f.fix_sketch}")
            lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Finding conversion (reuses onboarding's converter pattern)
# ---------------------------------------------------------------------------
def _convert_findings(llm_findings: list) -> list[Finding]:
    out: list[Finding] = []
    for llm_f in llm_findings:
        if not llm_f.file or llm_f.line <= 0:
            logger.warning(
                "dropping_review_finding_missing_citation description=%s",
                llm_f.description[:80],
            )
            continue
        out.append(
            Finding(
                file=llm_f.file,
                line=llm_f.line,
                severity=llm_f.severity or "medium",
                kind=llm_f.kind or "quality",
                description=llm_f.description,
                fix_sketch=llm_f.fix_sketch,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
class PRReviewError(RuntimeError):
    """Raised when the PR review pipeline cannot produce a valid result."""


async def run_pr_review(
    session: AsyncSession,
    repo_name: str,
    pr_info: PRInfo,
    diff_hunks: list[DiffHunk],
    *,
    llm: LLMClient,
    max_files: int = _MAX_FILES_PER_REVIEW,
) -> PRReviewResult:
    """Run the two-stage PR review recipe.

    The caller provides ``pr_info`` + ``diff_hunks`` (fetched from the
    GitHub API). The recipe handles context lookup, LLM calls, guardrails,
    and confidence blending.
    """
    repo = await resolve_repo(session, repo_name)

    # Cap the number of files we review (huge PRs get truncated).
    if len(diff_hunks) > max_files:
        logger.warning(
            "pr_review_truncated pr=%d files=%d cap=%d",
            pr_info.number,
            len(diff_hunks),
            max_files,
        )
        # Keep the top N by additions count (most changed = most risky).
        diff_hunks = sorted(
            diff_hunks, key=lambda h: h.additions, reverse=True
        )[:max_files]

    # Stage 1: build context for each changed file (deterministic).
    diff_context = await diff_context_view(session, repo_name, diff_hunks)

    # Stage 2: LLM analyzes the diff + context → findings.
    review_prompt = _load_prompt("review_changes.md")
    file_blocks = "\n".join(
        _render_file_for_prompt(ctx) for ctx in diff_context.files
    )
    review_user = (
        f"PR #{pr_info.number}: {pr_info.title}\n"
        f"Author: {pr_info.author}\n"
        f"Base: {pr_info.base_ref} ← Head: {pr_info.head_ref}\n\n"
    )
    if pr_info.body:
        review_user += f"Description:\n{pr_info.body}\n\n"
    review_user += (
        f"Changed files ({diff_context.total_count}, "
        f"{diff_context.indexed_count} indexed):\n\n{file_blocks}"
    )

    findings_response = await llm.call(
        system=review_prompt,
        user=review_user,
        response_schema=FindingsResponse,
        max_tokens=_REVIEW_MAX_TOKENS,
    )
    assert isinstance(findings_response.parsed, FindingsResponse)
    findings = _convert_findings(findings_response.parsed.findings)

    # Stage 2.5: architectural guardrails.
    # Pass diff_hunks so the line-range check accepts lines added by the PR
    # even though they're beyond the indexed file length.
    original_count = len(findings)
    if findings:
        findings, dropped = await verify_findings(
            session, repo.id, findings, diff_hunks=diff_hunks
        )
        if dropped:
            logger.info(
                "pr_review_guardrails pr=%d dropped=%d kept=%d",
                pr_info.number,
                len(dropped),
                len(findings),
            )

    # Stage 3: LLM summarizes verified findings → review.
    summary_prompt = _load_prompt("review_summary.md")
    summary_user = _render_findings_for_summary(
        findings, pr_info.title, diff_context.total_count
    )
    summary_response = await llm.call(
        system=summary_prompt,
        user=summary_user,
        response_schema=ReviewSummaryResponse,
        max_tokens=_SUMMARY_MAX_TOKENS,
    )
    assert isinstance(summary_response.parsed, ReviewSummaryResponse)
    summary_data = summary_response.parsed

    # Validate verdict.
    valid_verdicts = {"approve", "comment", "request_changes"}
    verdict = summary_data.verdict.lower().strip()
    if verdict not in valid_verdicts:
        verdict = "comment"  # safe fallback

    # Blend LLM confidence with structural pass rate.
    llm_confidence = max(0.0, min(1.0, summary_data.confidence))
    final_confidence = structural_confidence(
        original_count=original_count,
        verified_count=len(findings),
        llm_confidence=llm_confidence,
    )

    return PRReviewResult(
        repo_name=repo_name,
        pr_number=pr_info.number,
        pr_title=pr_info.title,
        summary=summary_data.summary,
        verdict=verdict,
        findings=findings,
        confidence=final_confidence,
    )
