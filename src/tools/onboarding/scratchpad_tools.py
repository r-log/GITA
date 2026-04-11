"""
Scratchpad-backed tools for the onboarding review loop.

These tools are scoped to a single onboarding run — each factory closes over
a per-run `scratchpad` dict. They're kept out of `src/tools/db/` because
other agents shouldn't import them (no use outside onboarding's explorer loop).

Two tools:
  - record_finding(file, line, severity, kind, finding, fix_sketch)
      Append a concrete code-review finding to scratchpad["findings"].
      Validates that `file` exists in the repo's code_index and that `line`
      is within bounds, so the LLM can't cite fabricated positions.

  - finalize_exploration(project_summary, confidence)
      End the explorer loop. Sets scratchpad["finalized"] = True so the
      orchestrator knows to stop iterating.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select

from src.core.database import async_session
from src.models.code_index import CodeIndex
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


_MAX_FINDINGS = 30
_VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_VALID_KINDS = {
    "bug", "error_handling", "security", "performance",
    "abstraction", "duplication", "dead_code", "correctness",
    "missing_validation", "resource_leak", "race_condition",
    "deprecated_api", "other",
}
_BANNED_FINDING_PATTERNS = (
    "add unit tests", "add tests", "needs tests", "write tests",
    "add ci/cd", "add continuous integration", "set up ci",
    "improve documentation", "add docs", "needs docs",
    "add logging", "needs logging",
    "add type hints", "add readme",
)


async def _file_exists_in_index(repo_id: int, file_path: str) -> tuple[bool, int | None]:
    """Return (exists, line_count) for a file in code_index."""
    async with async_session() as session:
        result = await session.execute(
            select(CodeIndex.line_count).where(
                CodeIndex.repo_id == repo_id,
                CodeIndex.file_path == file_path,
            )
        )
        row = result.first()
    if row is None:
        return False, None
    return True, row[0]


# ── record_finding ────────────────────────────────────────────────


def make_record_finding(repo_id: int, scratchpad: dict[str, Any]) -> Tool:
    """
    Factory that returns a record_finding tool closed over the scratchpad.
    Appends to scratchpad["findings"].
    """
    scratchpad.setdefault("findings", [])

    async def _handler(
        file: str,
        line: int,
        severity: str,
        kind: str,
        finding: str,
        fix_sketch: str = "",
    ) -> ToolResult:
        findings = scratchpad["findings"]
        if len(findings) >= _MAX_FINDINGS:
            return ToolResult(
                success=False,
                error=(
                    f"finding cap reached ({_MAX_FINDINGS}). "
                    "Finalize the exploration with finalize_exploration."
                ),
            )

        # Validate severity + kind up-front so the LLM self-corrects
        if severity not in _VALID_SEVERITIES:
            return ToolResult(
                success=False,
                error=f"invalid severity '{severity}'. Use one of: {sorted(_VALID_SEVERITIES)}",
            )
        if kind not in _VALID_KINDS:
            return ToolResult(
                success=False,
                error=f"invalid kind '{kind}'. Use one of: {sorted(_VALID_KINDS)}",
            )

        # Ban generic findings only when the finding is short (< 80 chars)
        # AND dominated by a generic phrase. Longer findings are assumed to
        # be specific enough — the banned word might just be descriptive
        # context ("swallows exceptions without logging the filename").
        finding_stripped = finding.strip()
        if len(finding_stripped) < 80:
            finding_lower = finding_stripped.lower()
            for pattern in _BANNED_FINDING_PATTERNS:
                if pattern in finding_lower:
                    return ToolResult(
                        success=False,
                        error=(
                            f"finding rejected: it's short ({len(finding_stripped)} chars) "
                            f"and dominated by the generic phrase '{pattern}'. "
                            f"Expand it to describe the specific code problem at "
                            f"{file}:{line} — what is actually wrong in the code, "
                            f"not what to add."
                        ),
                    )

        # Verify the file exists and the line number is plausible.
        exists, line_count = await _file_exists_in_index(repo_id, file)
        if not exists:
            return ToolResult(
                success=False,
                error=(
                    f"file '{file}' is not in the code index. "
                    "Call list_project_files to see valid paths."
                ),
            )
        if line_count and line < 1:
            return ToolResult(
                success=False,
                error=f"line must be >= 1, got {line}",
            )
        if line_count and line > line_count:
            return ToolResult(
                success=False,
                error=(
                    f"line {line} is out of bounds — {file} has only "
                    f"{line_count} lines. Re-check the file with "
                    f"get_code_slice or read_file."
                ),
            )

        entry = {
            "id": len(findings) + 1,
            "file": file,
            "line": line,
            "severity": severity,
            "kind": kind,
            "finding": finding.strip(),
            "fix_sketch": fix_sketch.strip() if fix_sketch else "",
        }
        findings.append(entry)

        log.info(
            "finding_recorded",
            id=entry["id"],
            file=file,
            line=line,
            severity=severity,
            kind=kind,
        )

        return ToolResult(
            success=True,
            data={
                "id": entry["id"],
                "total_findings": len(findings),
                "remaining_slots": _MAX_FINDINGS - len(findings),
            },
        )

    return Tool(
        name="record_finding",
        description=(
            "Record a concrete code-review finding with a file:line citation. "
            "Required fields: file, line, severity, kind, finding. "
            f"Severity must be one of: {sorted(_VALID_SEVERITIES)}. "
            f"Kind must be one of: {sorted(_VALID_KINDS)}. "
            "Findings that contain generic phrases like 'add tests', 'add docs', "
            "or 'add CI/CD' are rejected — cite a specific code problem instead. "
            f"Max {_MAX_FINDINGS} findings per run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "File path from the code map"},
                "line": {"type": "integer", "description": "1-indexed line where the problem is"},
                "severity": {
                    "type": "string",
                    "enum": sorted(_VALID_SEVERITIES),
                },
                "kind": {
                    "type": "string",
                    "enum": sorted(_VALID_KINDS),
                },
                "finding": {
                    "type": "string",
                    "description": "Concrete description of the specific problem",
                },
                "fix_sketch": {
                    "type": "string",
                    "description": "Optional: a short hint at how to fix it",
                },
            },
            "required": ["file", "line", "severity", "kind", "finding"],
        },
        handler=_handler,
    )


# ── finalize_exploration ──────────────────────────────────────────


def make_finalize_exploration(scratchpad: dict[str, Any]) -> Tool:
    """
    Factory for the loop-termination tool. LLM calls this when done exploring.
    Sets scratchpad["finalized"] = True and stores the project summary.
    """

    def _handler(project_summary: str, confidence: float = 0.7) -> ToolResult:
        if not project_summary or not project_summary.strip():
            return ToolResult(
                success=False,
                error="project_summary is required — write 2-4 sentences describing the project.",
            )
        if not 0.0 <= confidence <= 1.0:
            return ToolResult(
                success=False,
                error="confidence must be between 0.0 and 1.0",
            )

        scratchpad["finalized"] = True
        scratchpad["project_summary"] = project_summary.strip()
        scratchpad["exploration_confidence"] = confidence

        findings_count = len(scratchpad.get("findings", []))
        log.info(
            "exploration_finalized",
            findings=findings_count,
            confidence=confidence,
        )

        return ToolResult(
            success=True,
            data={
                "finalized": True,
                "findings_recorded": findings_count,
            },
        )

    return Tool(
        name="finalize_exploration",
        description=(
            "Call this when you've finished exploring and are ready to hand off "
            "findings for grouping. Pass a 2-4 sentence project_summary describing "
            "what the project IS (tech stack, purpose, architecture shape) plus "
            "your confidence 0.0-1.0. This ENDS the exploration loop — make sure "
            "all findings are recorded first."
        ),
        parameters={
            "type": "object",
            "properties": {
                "project_summary": {
                    "type": "string",
                    "description": "2-4 sentence description of what the project is",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence in your exploration 0.0-1.0",
                },
            },
            "required": ["project_summary"],
        },
        handler=_handler,
    )
