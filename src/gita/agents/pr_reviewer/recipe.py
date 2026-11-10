"""Two-stage PR review recipe.

Pipeline:
    1. diff_context_view(session, repo, hunks)           deterministic
    2. LLM: "given the diff + context, find issues"       call 1
    3. verify_findings(session, repo_id, findings)         deterministic (guardrails)
    4. decide_verdict(verified findings, coverage)         deterministic
    5. LLM: "summarize the verified findings"              call 2
    6. structural_confidence blending                       deterministic

Trust boundaries:
    * Everything the PR author controls (title, description, branch
      names, the diff) and everything read from the repository is
      wrapped in ``<untrusted_{nonce}>`` blocks. The nonce is random per
      request, so text inside a block cannot forge the closing tag.
    * The model proposes findings; code decides which survive
      (guardrails) and what the verdict is. The model never chooses
      "approve".
    * The review states what it did not cover: files that were binary,
      too large, over the file cap or over the prompt budget are listed
      as unreviewed, and a review with unreviewed files or discarded
      findings can't be an approval.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.guardrails import structural_confidence, verify_findings
from gita.agents.onboarding.schemas import FindingsResponse, LLMFinding
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
# A single file's patch above this is not shown in part: half a diff
# invites confident findings about the half that's missing.
_PATCH_CAP_CHARS = 20_000
_PR_BODY_CAP_CHARS = 4_000
# Total budget for the per-file blocks of the review prompt (~35k tokens).
_PROMPT_BUDGET_CHARS = 140_000

_BLOCKING_SEVERITIES = frozenset({"high", "critical"})


class PRReviewError(RuntimeError):
    """Raised when the PR review pipeline cannot produce a valid result."""


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------
def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Untrusted-content fencing
# ---------------------------------------------------------------------------
def _new_nonce() -> str:
    return secrets.token_hex(6)


def fence(text: str, *, nonce: str, source: str) -> str:
    """Wrap attacker-controllable text in a block it can't close itself."""
    return f'<untrusted_{nonce} source="{source}">\n{text}\n</untrusted_{nonce}>'


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------
def _prepend_line_numbers(content: str) -> str:
    # split("\n"), not splitlines(): the latter also breaks on form feeds,
    # U+2028 and friends, which shifts every later number relative to
    # how git, GitHub and the indexer count lines.
    return "\n".join(
        f"{i:>4}: {line}" for i, line in enumerate(content.split("\n"), start=1)
    )


def _render_file_block(ctx: FileContext) -> str:
    """Render one changed file's diff + context (unfenced)."""
    hunk = ctx.diff_hunk
    lines: list[str] = [
        f"=== {hunk.file_path} ({hunk.status}, "
        f"+{hunk.additions}/-{hunk.deletions}) ===",
        "",
        "DIFF:",
        hunk.patch or "",
    ]

    if ctx.symbols_near_changes:
        lines.append("")
        lines.append("SYMBOLS NEAR CHANGES:")
        for sym in ctx.symbols_near_changes:
            parent = f" in {sym.parent_class}" if sym.parent_class else ""
            lines.append(
                f"  {sym.kind} {sym.name}{parent} "
                f"(lines {sym.start_line}-{sym.end_line})"
            )

    if ctx.imported_by:
        lines.append("")
        lines.append(
            f"IMPORTED BY ({len(ctx.imported_by)} files): "
            + ", ".join(ctx.imported_by[:5])
        )
        if len(ctx.imported_by) > 5:
            lines.append(f"  ... and {len(ctx.imported_by) - 5} more")

    if ctx.content:
        content = ctx.content
        lines.append("")
        if len(content) > _FILE_CONTEXT_CAP_CHARS:
            content = content[:_FILE_CONTEXT_CAP_CHARS]
            lines.append(
                f"FILE CONTEXT (first {_FILE_CONTEXT_CAP_CHARS} chars, "
                f"{ctx.line_count} lines total):"
            )
        else:
            lines.append(f"FILE CONTEXT ({ctx.line_count} lines):")
        lines.append(_prepend_line_numbers(content))

    return "\n".join(lines)


@dataclass
class _ReviewPrompt:
    text: str
    reviewed_paths: list[str] = field(default_factory=list)
    unreviewed_paths: list[str] = field(default_factory=list)


def build_review_prompt(
    pr_info: PRInfo,
    files: list[FileContext],
    *,
    nonce: str,
    budget_chars: int = _PROMPT_BUDGET_CHARS,
) -> _ReviewPrompt:
    """Assemble the review prompt and record which files made it in.

    A file is unreviewed when it has no patch (binary, or too large for
    GitHub to return one), its patch exceeds ``_PATCH_CAP_CHARS``, or
    including it would exceed ``budget_chars``.
    """
    body = pr_info.body or ""
    if len(body) > _PR_BODY_CAP_CHARS:
        body = body[:_PR_BODY_CAP_CHARS] + "\n... (description truncated)"
    meta = (
        f"PR #{pr_info.number}: {pr_info.title}\n"
        f"Author: {pr_info.author}\n"
        f"Base: {pr_info.base_ref} <- Head: {pr_info.head_ref}\n"
        + (f"\nDescription:\n{body}" if body else "")
    )

    prompt = _ReviewPrompt(text="")
    blocks: list[str] = []
    used = 0
    for ctx in files:
        patch = ctx.diff_hunk.patch
        if not patch or len(patch) > _PATCH_CAP_CHARS:
            prompt.unreviewed_paths.append(ctx.file_path)
            continue
        block = fence(_render_file_block(ctx), nonce=nonce, source="changed_file")
        if used + len(block) > budget_chars:
            prompt.unreviewed_paths.append(ctx.file_path)
            continue
        blocks.append(block)
        used += len(block)
        prompt.reviewed_paths.append(ctx.file_path)

    prompt.text = (
        f"Untrusted blocks in this message are delimited by "
        f"<untrusted_{nonce}> ... </untrusted_{nonce}>.\n\n"
        + fence(meta, nonce=nonce, source="pr_metadata")
        + f"\n\nChanged files shown ({len(blocks)} of {len(files)}):\n\n"
        + "\n\n".join(blocks)
    )
    return prompt


def _render_findings_for_summary(
    findings: list[Finding],
    *,
    pr_title: str,
    total_files: int,
    verdict: str,
    dropped: int,
    unreviewed: int,
    nonce: str,
) -> str:
    lines = [
        f"Verdict (already decided, do not change it): {verdict}",
        f"Files changed: {total_files} ({unreviewed} not reviewed)",
        f"Findings discarded by verification: {dropped}",
        "",
        fence(f"PR title: {pr_title}", nonce=nonce, source="pr_metadata"),
        "",
        f"Verified findings ({len(findings)}):",
    ]
    if not findings:
        lines.append("(none in the reviewed changes)")
    else:
        rendered = []
        for i, f in enumerate(findings):
            entry = (
                f"[{i}] {f.severity} {f.kind} {f.file}:{f.line}\n"
                f"    {f.description}"
            )
            if f.fix_sketch:
                entry += f"\n    fix: {f.fix_sketch}"
            rendered.append(entry)
        lines.append(fence("\n".join(rendered), nonce=nonce, source="findings"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deterministic decisions
# ---------------------------------------------------------------------------
def decide_verdict(
    findings: list[Finding], *, dropped: int, unreviewed: int
) -> str:
    """The review verdict, from verified findings and coverage only.

    * ``request_changes`` — any verified high/critical finding.
    * ``approve`` — no findings, nothing discarded, every file reviewed.
    * ``comment`` — everything else, including "nothing found, but the
      review didn't see everything".
    """
    if any(f.severity in _BLOCKING_SEVERITIES for f in findings):
        return "request_changes"
    if not findings and dropped == 0 and unreviewed == 0:
        return "approve"
    return "comment"


def _convert_findings(llm_findings: list[LLMFinding]) -> list[Finding]:
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
                severity=llm_f.severity,
                kind=llm_f.kind,
                description=llm_f.description,
                fix_sketch=llm_f.fix_sketch,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
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
    the verdict, and confidence blending.
    """
    repo = await resolve_repo(session, repo_name)
    nonce = _new_nonce()

    # Cap the number of files we review; the rest are reported, not hidden.
    over_cap: list[str] = []
    if len(diff_hunks) > max_files:
        ranked = sorted(diff_hunks, key=lambda h: h.additions, reverse=True)
        diff_hunks, over_cap = ranked[:max_files], [h.file_path for h in ranked[max_files:]]
        logger.warning(
            "pr_review_truncated pr=%d files=%d cap=%d",
            pr_info.number,
            len(diff_hunks) + len(over_cap),
            max_files,
        )

    # Stage 1: context for each changed file (deterministic).
    diff_context = await diff_context_view(session, repo_name, diff_hunks)

    # Stage 2: model proposes findings.
    prompt = build_review_prompt(pr_info, diff_context.files, nonce=nonce)
    unreviewed = over_cap + prompt.unreviewed_paths
    total_files = len(diff_hunks) + len(over_cap)

    findings: list[Finding] = []
    original_count = 0
    if prompt.reviewed_paths:
        findings_response = await llm.call(
            system=_load_prompt("review_changes.md"),
            user=prompt.text,
            response_schema=FindingsResponse,
            max_tokens=_REVIEW_MAX_TOKENS,
        )
        if not isinstance(findings_response.parsed, FindingsResponse):
            raise PRReviewError("findings call returned no parsed response")
        findings = _convert_findings(findings_response.parsed.findings)
        original_count = len(findings)

    # Stage 3: guardrails. Lines added by the PR are beyond the indexed
    # file length, so the diff ranges are passed to relax that check.
    dropped = 0
    if findings:
        findings, dropped_list = await verify_findings(
            session, repo.id, findings, diff_hunks=diff_hunks
        )
        dropped = len(dropped_list)
        if dropped:
            logger.info(
                "pr_review_guardrails pr=%d dropped=%d kept=%d",
                pr_info.number,
                dropped,
                len(findings),
            )

    # Stage 4: verdict, decided in code.
    verdict = decide_verdict(findings, dropped=dropped, unreviewed=len(unreviewed))

    # Stage 5: model writes the summary for that verdict.
    summary_response = await llm.call(
        system=_load_prompt("review_summary.md"),
        user=_render_findings_for_summary(
            findings,
            pr_title=pr_info.title,
            total_files=total_files,
            verdict=verdict,
            dropped=dropped,
            unreviewed=len(unreviewed),
            nonce=nonce,
        ),
        response_schema=ReviewSummaryResponse,
        max_tokens=_SUMMARY_MAX_TOKENS,
    )
    if not isinstance(summary_response.parsed, ReviewSummaryResponse):
        raise PRReviewError("summary call returned no parsed response")
    summary_data = summary_response.parsed

    # Stage 6: the model's self-assessment can only lower confidence;
    # the guardrail pass rate lowers it further.
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
        dropped_findings=dropped,
        unreviewed_files=unreviewed,
    )
