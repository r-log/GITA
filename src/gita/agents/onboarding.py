"""Onboarding agent — the first real consumer of the view layer + LLM client.

**Reading recipe** (three LLM calls, rigid, bounded):

    1. load_bearing_view(repo, limit=10)                     deterministic
    2. LLM: "which 3–5 of these should I read deeply?"       call 1
    3. fetch file contents for the picks                      deterministic
    4. LLM: "given these file bodies, produce findings"      call 2
    5. LLM: "group findings into 0–5 milestones"             call 3

No free-form tool loop. No "let the LLM decide its next move." The recipe
is code, the LLM is confined to judgment at pre-defined forks. This is the
v1-failure fix: v1 let the LLM drive its own exploration and it wandered
into generic territory.

Every LLM call carries a pydantic schema. Invalid responses raise
``LLMSchemaError`` — callers decide whether to retry. Day 5 ships with no
retry; Day 6 adds it if prompt iteration shows we need it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.types import Finding, Milestone, OnboardingResult
from gita.db.models import CodeIndex
from gita.llm.client import LLMClient
from gita.views._common import SymbolBrief, build_symbol_summary, resolve_repo
from gita.views.load_bearing import load_bearing_view

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Size cap per file body handed to the LLM. Keeps a 2000-line file from
# blowing up stage 2's input. Picked conservatively for ~3k tokens.
_FILE_BODY_CAP_CHARS = 12_000
_DEFAULT_LOAD_BEARING_LIMIT = 10
_DEFAULT_DEEP_READ_LIMIT = 5
_FINDINGS_MAX_TOKENS = 4096
_GROUPING_MAX_TOKENS = 2048
_PICK_MAX_TOKENS = 1024


# ---------------------------------------------------------------------------
# LLM I/O schemas (pydantic models — used to validate response_format JSON)
# ---------------------------------------------------------------------------
class PickFilesResponse(BaseModel):
    project_summary: str = Field(
        description="2-3 sentence description of what the project is"
    )
    tech_stack: list[str] = Field(
        default_factory=list,
        description="Language, framework, notable libraries",
    )
    picks: list[int] = Field(
        description="0-based indices into the load_bearing list"
    )
    reasoning: str = ""


class LLMFinding(BaseModel):
    file: str
    line: int
    severity: str
    kind: str
    description: str
    fix_sketch: str = ""


class FindingsResponse(BaseModel):
    findings: list[LLMFinding] = Field(default_factory=list)


class LLMMilestone(BaseModel):
    title: str
    summary: str
    finding_indices: list[int] = Field(default_factory=list)
    confidence: float = 0.0


class MilestonesResponse(BaseModel):
    milestones: list[LLMMilestone] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------
def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------
@dataclass
class _RankedFileForPrompt:
    index: int
    file_path: str
    language: str
    line_count: int
    in_degree: int
    symbol_summary: list[SymbolBrief]


@dataclass
class _FileBody:
    file_path: str
    language: str
    line_count: int
    content: str
    truncated: bool
    symbol_summary: list[SymbolBrief]


def _render_load_bearing_for_prompt(
    files: list[_RankedFileForPrompt],
) -> str:
    lines = []
    for ranked in files:
        header = (
            f"[{ranked.index}] {ranked.file_path}  "
            f"({ranked.language}, {ranked.line_count} lines, "
            f"in_degree={ranked.in_degree})"
        )
        lines.append(header)
        shown = ranked.symbol_summary[:12]
        for brief in shown:
            parent = (
                f" in {brief.parent_class}" if brief.parent_class else ""
            )
            lines.append(
                f"    line {brief.line:>4}  {brief.kind:<16} "
                f"{brief.name}{parent}"
            )
        if len(ranked.symbol_summary) > len(shown):
            lines.append(
                f"    ... and {len(ranked.symbol_summary) - len(shown)} more"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_file_bodies_for_prompt(bodies: list[_FileBody]) -> str:
    chunks: list[str] = []
    for body in bodies:
        chunks.append(
            f"=== {body.file_path} ({body.language}, {body.line_count} lines) ==="
        )
        chunks.append(_prepend_line_numbers(body.content, body.line_count))
        if body.truncated:
            chunks.append("... [file truncated to keep the prompt bounded]")
        chunks.append("")
    return "\n".join(chunks).rstrip()


def _prepend_line_numbers(content: str, total_lines: int) -> str:
    width = len(str(max(total_lines, 1)))
    out = []
    for i, line in enumerate(content.splitlines(), start=1):
        out.append(f"{i:>{width}}: {line}")
    return "\n".join(out)


async def _fetch_file_bodies(
    session: AsyncSession,
    repo_id: Any,
    file_paths: list[str],
) -> list[_FileBody]:
    if not file_paths:
        return []
    stmt = (
        select(CodeIndex)
        .where(CodeIndex.repo_id == repo_id)
        .where(CodeIndex.file_path.in_(file_paths))
    )
    rows = {
        row.file_path: row
        for row in (await session.execute(stmt)).scalars().all()
    }
    bodies: list[_FileBody] = []
    for path in file_paths:  # preserve pick order
        row = rows.get(path)
        if row is None or row.content is None:
            logger.warning("onboarding_missing_file_body path=%s", path)
            continue
        content = row.content
        truncated = False
        if len(content) > _FILE_BODY_CAP_CHARS:
            content = content[:_FILE_BODY_CAP_CHARS]
            truncated = True
        bodies.append(
            _FileBody(
                file_path=row.file_path,
                language=row.language,
                line_count=row.line_count,
                content=content,
                truncated=truncated,
                symbol_summary=build_symbol_summary(row.structure or {}),
            )
        )
    return bodies


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
class OnboardingError(RuntimeError):
    """Raised when the onboarding pipeline cannot produce a valid result."""


async def run_onboarding(
    session: AsyncSession,
    repo_name: str,
    *,
    llm: LLMClient,
    load_bearing_limit: int = _DEFAULT_LOAD_BEARING_LIMIT,
    deep_read_limit: int = _DEFAULT_DEEP_READ_LIMIT,
) -> OnboardingResult:
    """Run the three-stage onboarding recipe for ``repo_name``.

    Caller owns the GithubClient / write mode. This function only produces
    an :class:`OnboardingResult`; it never touches GitHub. The Day 7 flow
    is: produce the result here, review it by hand, *then* wrap it in a
    Decision and post via ``execute_decision``.
    """
    repo = await resolve_repo(session, repo_name)

    # ------------------------------------------------------------------
    # Stage 0: load load-bearing files deterministically
    # ------------------------------------------------------------------
    load_bearing = await load_bearing_view(
        session, repo_name, limit=load_bearing_limit
    )
    if not load_bearing.files:
        raise OnboardingError(
            f"repo {repo_name!r} has no indexed files — run `gita index` first"
        )

    ranked_for_prompt = [
        _RankedFileForPrompt(
            index=i,
            file_path=f.file_path,
            language=f.language,
            line_count=f.line_count,
            in_degree=f.in_degree,
            symbol_summary=f.symbol_summary,
        )
        for i, f in enumerate(load_bearing.files)
    ]

    # ------------------------------------------------------------------
    # Stage 1: LLM picks which files to read deeply
    # ------------------------------------------------------------------
    pick_prompt = _load_prompt("onboarding_pick_files.md")
    pick_user = (
        f"Repo: {repo_name}\n\n"
        f"Load-bearing files ({len(ranked_for_prompt)}):\n\n"
        f"{_render_load_bearing_for_prompt(ranked_for_prompt)}\n\n"
        f"Pick up to {deep_read_limit} file indices to read deeply."
    )
    pick_response = await llm.call(
        system=pick_prompt,
        user=pick_user,
        response_schema=PickFilesResponse,
        max_tokens=_PICK_MAX_TOKENS,
    )
    assert isinstance(pick_response.parsed, PickFilesResponse)
    pick_data: PickFilesResponse = pick_response.parsed

    # Validate and clamp picks
    valid_picks: list[int] = []
    seen_picks: set[int] = set()
    for idx in pick_data.picks:
        if idx in seen_picks:
            continue
        if 0 <= idx < len(load_bearing.files):
            valid_picks.append(idx)
            seen_picks.add(idx)
        if len(valid_picks) >= deep_read_limit:
            break

    if not valid_picks:
        # LLM picked nothing or all invalid — fall back to the top 3 by rank.
        valid_picks = list(range(min(3, len(load_bearing.files))))
        logger.warning(
            "onboarding_fallback_picks repo=%s reason=empty_or_invalid", repo_name
        )

    picked_paths = [load_bearing.files[i].file_path for i in valid_picks]

    # ------------------------------------------------------------------
    # Stage 2: fetch file bodies (deterministic, no LLM)
    # ------------------------------------------------------------------
    bodies = await _fetch_file_bodies(session, repo.id, picked_paths)

    # ------------------------------------------------------------------
    # Stage 3: LLM extracts findings from file bodies
    # ------------------------------------------------------------------
    findings: list[Finding] = []
    if bodies:
        findings_prompt = _load_prompt("onboarding_findings.md")
        findings_user = (
            f"Project summary (from the previous step):\n"
            f"{pick_data.project_summary}\n\n"
            f"Tech stack: {', '.join(pick_data.tech_stack) or 'unknown'}\n\n"
            f"Files to review ({len(bodies)}):\n\n"
            f"{_render_file_bodies_for_prompt(bodies)}"
        )
        findings_response = await llm.call(
            system=findings_prompt,
            user=findings_user,
            response_schema=FindingsResponse,
            max_tokens=_FINDINGS_MAX_TOKENS,
        )
        assert isinstance(findings_response.parsed, FindingsResponse)
        findings = _convert_findings(findings_response.parsed.findings)

    # ------------------------------------------------------------------
    # Stage 4: LLM groups findings into milestones
    # ------------------------------------------------------------------
    milestones: list[Milestone] = []
    if findings:
        group_prompt = _load_prompt("onboarding_group.md")
        group_user = _render_findings_for_grouping(findings)
        group_response = await llm.call(
            system=group_prompt,
            user=group_user,
            response_schema=MilestonesResponse,
            max_tokens=_GROUPING_MAX_TOKENS,
        )
        assert isinstance(group_response.parsed, MilestonesResponse)
        milestones = _convert_milestones(
            group_response.parsed.milestones, len(findings)
        )

    return OnboardingResult(
        repo_name=repo_name,
        project_summary=pick_data.project_summary,
        findings=findings,
        milestones=milestones,
        confidence=_overall_confidence(findings, milestones),
    )


# ---------------------------------------------------------------------------
# LLM → dataclass conversion (keeps the two type systems isolated)
# ---------------------------------------------------------------------------
def _convert_findings(llm_findings: list[LLMFinding]) -> list[Finding]:
    out: list[Finding] = []
    for llm_f in llm_findings:
        if not llm_f.file or llm_f.line <= 0:
            logger.warning(
                "dropping_finding_missing_citation description=%s",
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


def _convert_milestones(
    llm_milestones: list[LLMMilestone],
    n_findings: int,
) -> list[Milestone]:
    out: list[Milestone] = []
    for llm_m in llm_milestones:
        valid_indices = [
            i for i in llm_m.finding_indices if 0 <= i < n_findings
        ]
        if not valid_indices:
            logger.warning(
                "dropping_milestone_no_valid_findings title=%s", llm_m.title
            )
            continue
        out.append(
            Milestone(
                title=llm_m.title,
                summary=llm_m.summary,
                finding_indices=valid_indices,
                confidence=max(0.0, min(1.0, llm_m.confidence)),
            )
        )
    return out


def _render_findings_for_grouping(findings: list[Finding]) -> str:
    lines = [f"Findings ({len(findings)}):", ""]
    for i, f in enumerate(findings):
        lines.append(
            f"[{i}] {f.severity:<8} {f.kind:<10} {f.file}:{f.line}"
        )
        lines.append(f"    {f.description}")
        if f.fix_sketch:
            lines.append(f"    fix: {f.fix_sketch}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _overall_confidence(
    findings: list[Finding], milestones: list[Milestone]
) -> float:
    if not milestones:
        # No milestones = no claims to defend. Confidence is neutral, not 0.
        return 0.5 if findings else 0.3
    avg = sum(m.confidence for m in milestones) / len(milestones)
    return max(0.0, min(1.0, avg))
