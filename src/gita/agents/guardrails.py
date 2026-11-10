"""Architectural guardrails for agent findings.

These are **walls, not rules**. The LLM produces whatever it produces;
this module verifies each finding against structural ground truth from the
index and drops anything that fails. The LLM's output is treated as
untrusted input — same way you'd validate user input from a web form.

The key invariant: **every finding that survives ``verify_findings()``
has been structurally confirmed against the code_index**. Prompts still
guide the LLM toward better output (reducing the filter volume), but
they are never the last line of defense.

Two entry points:

* ``verify_findings`` — onboarding. Ground truth is ``code_index``
  (the default branch), because that is what the model was shown.
* ``verify_pr_findings`` — PR review. Ground truth is the PR itself: the
  reviewed files, the lines the model was shown, and the head content.
  The index is the wrong reference there (new files aren't in it, and
  its line numbers are pre-PR).

``verify_findings`` guardrails (applied in order):
1. **File existence** — ``finding.file`` must exist in ``code_index``
   for this repo. Catches hallucinated file paths.
2. **Line range** — ``finding.line`` must be ≤ ``code_index.line_count``.
   Catches hallucinated line numbers.
3. **AST parse gate** — if the finding description claims a syntax/parse
   error (matched by regex), and the file parses cleanly, the finding
   is dropped. Catches the P6-class hallucination where the LLM flags
   valid multi-line Python as broken.
4. **Banned-phrase filter** — if the finding description matches a
   boilerplate regex (the same patterns the checklist uses at test time),
   the finding is dropped at runtime so it never reaches the bridge.

After filtering, the pass rate ``verified / original`` feeds into
``structural_confidence()``, which the recipe can blend with the LLM's
self-assessed confidence.
"""
from __future__ import annotations

import ast
import logging
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.types import Finding
from gita.db.models import CodeIndex

if TYPE_CHECKING:  # type-only: importing these at runtime would be circular
    from gita.views.diff_context import FileContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Banned-phrase patterns (runtime enforcement of the checklist rules).
# These are the same patterns that live in the golden-agent checklists,
# but applied here as a structural filter rather than a test-time check.
# ---------------------------------------------------------------------------
_BANNED_DESCRIPTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"add unit tests",
        r"add (?:more )?tests",
        r"improve (?:test )?coverage",
        r"set up (?:ci|cd)",
        r"add (?:ci|cd)",
        r"configure github actions",
        r"add (?:more )?documentation",
        r"improve docs",
        r"improve code quality",
        r"follow best practices",
    ]
]

# Regex patterns that identify a finding as claiming a syntax/parse error.
_SYNTAX_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"syntax\s*error",
        r"unclosed\s*paren",
        r"unparseable",
        r"cannot\s*parse",
        r"won'?t\s*parse",
        r"will\s*not\s*(?:parse|run)",
        r"missing\s*closing\s*(?:paren|bracket|brace)",
    ]
]


# ---------------------------------------------------------------------------
# Individual guardrail checks
# ---------------------------------------------------------------------------
def _check_banned_phrase(finding: Finding) -> str | None:
    """Return a reason string if the finding *is* boilerplate, else None.

    Only the description is checked. A real bug whose fix sketch ends
    with "...and add tests for the empty case" is still a real bug.
    """
    for pattern in _BANNED_DESCRIPTION_PATTERNS:
        if pattern.search(finding.description):
            return f"banned phrase: {pattern.pattern!r}"
    return None


def _claims_syntax_error(finding: Finding) -> bool:
    """True if the finding's description claims a syntax or parse error."""
    text = finding.description
    return any(pattern.search(text) for pattern in _SYNTAX_ERROR_PATTERNS)


def _file_parses_cleanly(content: str, language: str) -> bool:
    """Try to parse the file content. Only Python is supported for now
    (ast.parse); JS/TS would need tree-sitter which is heavier."""
    if language != "python":
        # Can't verify non-Python files structurally — give them the
        # benefit of the doubt. Future: add tree-sitter parse check.
        return True
    try:
        ast.parse(content)
        return True
    except SyntaxError:
        return False


# ---------------------------------------------------------------------------
# Main verification pass
# ---------------------------------------------------------------------------
async def verify_findings(
    session: AsyncSession,
    repo_id: Any,
    findings: list[Finding],
) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    """Verify each finding against structural ground truth.

    Returns ``(verified, dropped)`` where ``dropped`` is a list of
    ``(finding, reason)`` tuples. The recipe feeds ``verified`` into
    stage 4 (milestone grouping); ``dropped`` is logged for diagnostics.

    This function hits the DB once (batch fetch of all cited files) and
    then runs pure checks in-memory. Cost: one SQL query + negligible
    CPU for ast.parse on cached content.
    """
    if not findings:
        return [], []

    # Batch-fetch all cited files in one query.
    cited_paths = list({f.file for f in findings})
    stmt = (
        select(CodeIndex)
        .where(CodeIndex.repo_id == repo_id)
        .where(CodeIndex.file_path.in_(cited_paths))
    )
    rows = {
        row.file_path: row
        for row in (await session.execute(stmt)).scalars().all()
    }

    verified: list[Finding] = []
    dropped: list[tuple[Finding, str]] = []

    for finding in findings:
        # --- Guardrail 1: file existence ---
        row = rows.get(finding.file)
        if row is None:
            reason = f"file_not_found: {finding.file!r} not in code_index"
            dropped.append((finding, reason))
            logger.warning(
                "guardrail_drop reason=file_not_found file=%s",
                finding.file,
            )
            continue

        # --- Guardrail 2: line range ---
        if not 1 <= finding.line <= row.line_count:
            reason = (
                f"line_out_of_range: line {finding.line} not in 1..{row.line_count}"
            )
            dropped.append((finding, reason))
            logger.warning(
                "guardrail_drop reason=line_out_of_range file=%s line=%d max=%d",
                finding.file,
                finding.line,
                row.line_count,
            )
            continue

        # --- Guardrail 3: AST parse gate for syntax-error claims ---
        if _claims_syntax_error(finding):
            content = row.content or ""
            if _file_parses_cleanly(content, row.language):
                reason = (
                    f"syntax_claim_on_valid_file: LLM claimed syntax error "
                    f"but {finding.file} parses cleanly as {row.language}"
                )
                dropped.append((finding, reason))
                logger.warning(
                    "guardrail_drop reason=syntax_claim_invalid "
                    "file=%s line=%d language=%s",
                    finding.file,
                    finding.line,
                    row.language,
                )
                continue

        # --- Guardrail 4: banned-phrase filter ---
        banned_reason = _check_banned_phrase(finding)
        if banned_reason is not None:
            dropped.append((finding, banned_reason))
            logger.warning(
                "guardrail_drop reason=banned_phrase file=%s pattern=%s",
                finding.file,
                banned_reason,
            )
            continue

        # All checks passed — finding is verified.
        verified.append(finding)

    if dropped:
        logger.info(
            "guardrails_summary total=%d verified=%d dropped=%d",
            len(findings),
            len(verified),
            len(dropped),
        )

    return verified, dropped


# ---------------------------------------------------------------------------
# Structural confidence
# ---------------------------------------------------------------------------
def structural_confidence(
    original_count: int,
    verified_count: int,
    llm_confidence: float,
) -> float:
    """Blend the LLM's self-assessed confidence with a pass-rate penalty.

    If the LLM produced 7 findings and 2 got filtered, the structural
    signal is "28% of claims were wrong." That should drag down the
    overall confidence regardless of what the LLM thinks of itself.

    Formula: ``llm_confidence * pass_rate`` where
    ``pass_rate = verified / original`` (1.0 when nothing was filtered).

    Edge case: if the LLM produced 0 findings, pass_rate is 1.0 (nothing
    to filter ≠ low quality). If ALL findings were filtered, confidence
    bottoms at 0.1 (not 0.0 — the project summary might still be useful).
    """
    if original_count == 0:
        return llm_confidence
    pass_rate = verified_count / original_count
    blended = llm_confidence * pass_rate
    return max(0.1, min(1.0, blended))


# ---------------------------------------------------------------------------
# PR review guardrails — checked against the PR, not the index
# ---------------------------------------------------------------------------
def verify_pr_findings(
    findings: list[Finding],
    files: list[FileContext],
) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    """Verify PR findings against what the model was actually shown.

    The index describes the default branch, so it is the wrong ground
    truth for a PR: files the PR adds aren't in it, line numbers have
    moved, and a syntax error the PR introduces isn't there. Instead:

    1. **In the review** — the file must be one of the changed files the
       model was shown. Catches invented paths and citations of files
       outside the change.
    2. **Visible line** — the line must be one the model saw: inside a
       changed range of the diff (new side) or a numbered context window
       of the head content. Catches invented and out-of-range lines.
    3. **Syntax claims** — a claimed syntax error in a Python file is
       dropped only if the *head* content parses. If the head content is
       unknown, the claim is kept: we can't disprove it.
    4. **Boilerplate** — banned phrases in the description.

    Pure function: no database, no I/O.
    """
    by_path = {ctx.file_path: ctx for ctx in files}
    visible = {path: ctx.visible_lines() for path, ctx in by_path.items()}

    verified: list[Finding] = []
    dropped: list[tuple[Finding, str]] = []

    def drop(finding: Finding, reason: str) -> None:
        dropped.append((finding, reason))
        logger.warning(
            "pr_guardrail_drop file=%s line=%s reason=%s",
            finding.file,
            finding.line,
            reason,
        )

    for finding in findings:
        ctx = by_path.get(finding.file)
        if ctx is None:
            drop(finding, f"not_in_review: {finding.file!r} is not a reviewed file")
            continue
        if finding.line not in visible[finding.file]:
            drop(
                finding,
                f"line_not_shown: line {finding.line} of {finding.file} "
                "was not in the diff or context the model saw",
            )
            continue
        if (
            _claims_syntax_error(finding)
            and ctx.head_content is not None
            and ctx.language == "python"
            and _file_parses_cleanly(ctx.head_content, "python")
        ):
            drop(finding, "syntax_claim_on_valid_file: head version parses cleanly")
            continue
        banned = _check_banned_phrase(finding)
        if banned is not None:
            drop(finding, banned)
            continue
        verified.append(finding)

    return verified, dropped
