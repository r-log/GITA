"""Tests for the PR reviewer agent recipe.

Uses ``FakeLLMClient`` so the pipeline runs without touching OpenRouter.
The agent is wired against the real ``indexed_synth_py`` fixture —
ingested Postgres rows, real views, real guardrails — everything except
the LLM calls and the GitHub API fetches.

The test constructs ``PRInfo`` + ``DiffHunk`` objects by hand to simulate
a PR that modifies ``src/myapp/utils.py`` (touching ``format_name``).
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.onboarding.schemas import FindingsResponse, LLMFinding
from gita.agents.pr_reviewer.diff_parser import ChangedLineRange, DiffHunk
from gita.agents.pr_reviewer.recipe import PRReviewError, run_pr_review
from gita.agents.pr_reviewer.schemas import ReviewSummaryResponse
from gita.agents.types import PRReviewResult
from gita.github.client import PRInfo
from gita.llm.client import FakeLLMClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _pr_info(
    number: int = 42,
    title: str = "Fix format_name edge case",
    body: str = "Handles empty strings properly",
) -> PRInfo:
    return PRInfo(
        number=number,
        title=title,
        body=body,
        author="dev-alice",
        state="open",
        base_ref="main",
        head_ref="fix/format-name",
        head_sha="abc123",
        changed_files=1,
        additions=5,
        deletions=2,
        html_url="https://github.com/test/repo/pull/42",
    )


def _diff_hunks() -> list[DiffHunk]:
    """Simulate a PR that modifies utils.py at lines 1-2."""
    return [
        DiffHunk(
            file_path="src/myapp/utils.py",
            status="modified",
            additions=3,
            deletions=1,
            patch=(
                "@@ -1,2 +1,4 @@ \n"
                " def format_name(name: str) -> str:\n"
                "-    return name.strip().title()\n"
                "+    if not name or not name.strip():\n"
                "+        return ''\n"
                "+    return name.strip().title()\n"
            ),
            changed_ranges=[ChangedLineRange(start=1, count=4)],
        )
    ]


def _findings_response(
    findings: list[LLMFinding] | None = None,
) -> FindingsResponse:
    if findings is None:
        findings = [
            LLMFinding(
                file="src/myapp/utils.py",
                line=2,
                severity="low",
                kind="quality",
                description="Empty string check could use `not name.strip()` instead of double check",
                fix_sketch="Simplify to `if not name.strip():`",
            )
        ]
    return FindingsResponse(findings=findings)


def _summary_response(
    summary: str = "Minor style nit in format_name. No blocking issues.",
    verdict: str = "comment",
    confidence: float = 0.85,
) -> ReviewSummaryResponse:
    return ReviewSummaryResponse(
        summary=summary, verdict=verdict, confidence=confidence
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
class TestPRReviewHappyPath:
    async def test_produces_review_result(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        llm = FakeLLMClient(
            responses=[_findings_response(), _summary_response()]
        )

        result = await run_pr_review(
            session,
            repo_name,
            _pr_info(),
            _diff_hunks(),
            llm=llm,
        )

        assert isinstance(result, PRReviewResult)
        assert result.repo_name == repo_name
        assert result.pr_number == 42
        assert result.pr_title == "Fix format_name edge case"
        assert result.verdict == "comment"
        assert result.summary
        assert result.confidence > 0

    async def test_makes_two_llm_calls(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        llm = FakeLLMClient(
            responses=[_findings_response(), _summary_response()]
        )

        await run_pr_review(
            session, repo_name, _pr_info(), _diff_hunks(), llm=llm
        )

        assert len(llm.calls) == 2
        assert llm.calls[0]["schema"] == "FindingsResponse"
        assert llm.calls[1]["schema"] == "ReviewSummaryResponse"

    async def test_findings_converted_to_dataclass(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        llm = FakeLLMClient(
            responses=[_findings_response(), _summary_response()]
        )

        result = await run_pr_review(
            session, repo_name, _pr_info(), _diff_hunks(), llm=llm
        )

        assert len(result.findings) == 1
        f = result.findings[0]
        assert f.file == "src/myapp/utils.py"
        assert f.line == 2
        assert f.severity == "low"

    async def test_verdict_normalized_to_lowercase(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        llm = FakeLLMClient(
            responses=[
                _findings_response(),
                _summary_response(verdict="  REQUEST_CHANGES  "),
            ]
        )

        result = await run_pr_review(
            session, repo_name, _pr_info(), _diff_hunks(), llm=llm
        )
        assert result.verdict == "request_changes"

    async def test_invalid_verdict_falls_back_to_comment(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        llm = FakeLLMClient(
            responses=[
                _findings_response(),
                _summary_response(verdict="yolo"),
            ]
        )

        result = await run_pr_review(
            session, repo_name, _pr_info(), _diff_hunks(), llm=llm
        )
        assert result.verdict == "comment"


# ---------------------------------------------------------------------------
# Empty findings path
# ---------------------------------------------------------------------------
class TestPRReviewCleanPR:
    async def test_zero_findings_approved(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        llm = FakeLLMClient(
            responses=[
                _findings_response(findings=[]),
                _summary_response(
                    summary="No issues found.",
                    verdict="approve",
                    confidence=0.95,
                ),
            ]
        )

        result = await run_pr_review(
            session, repo_name, _pr_info(), _diff_hunks(), llm=llm
        )

        assert result.findings == []
        assert result.verdict == "approve"
        assert result.summary == "No issues found."


# ---------------------------------------------------------------------------
# Guardrails integration
# ---------------------------------------------------------------------------
class TestPRReviewGuardrails:
    async def test_finding_on_nonexistent_file_dropped(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """A finding citing a file not in code_index gets filtered."""
        session, repo_name = indexed_synth_py
        bad_finding = LLMFinding(
            file="src/ghost.py",
            line=10,
            severity="high",
            kind="bug",
            description="something bad in a hallucinated file",
        )
        llm = FakeLLMClient(
            responses=[
                _findings_response(findings=[bad_finding]),
                _summary_response(verdict="approve"),
            ]
        )

        result = await run_pr_review(
            session, repo_name, _pr_info(), _diff_hunks(), llm=llm
        )

        # The hallucinated finding was dropped by guardrails.
        assert result.findings == []

    async def test_finding_with_out_of_range_line_dropped(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        bad_finding = LLMFinding(
            file="src/myapp/utils.py",
            line=9999,
            severity="medium",
            kind="quality",
            description="something at a line that doesn't exist",
        )
        llm = FakeLLMClient(
            responses=[
                _findings_response(findings=[bad_finding]),
                _summary_response(verdict="approve"),
            ]
        )

        result = await run_pr_review(
            session, repo_name, _pr_info(), _diff_hunks(), llm=llm
        )
        assert result.findings == []

    async def test_structural_confidence_penalizes_filtered_findings(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """If half the findings get filtered, confidence drops."""
        session, repo_name = indexed_synth_py
        good = LLMFinding(
            file="src/myapp/utils.py",
            line=2,
            severity="low",
            kind="quality",
            description="minor issue",
        )
        bad = LLMFinding(
            file="src/ghost.py",
            line=1,
            severity="high",
            kind="bug",
            description="hallucinated",
        )
        llm = FakeLLMClient(
            responses=[
                _findings_response(findings=[good, bad]),
                _summary_response(confidence=0.9),
            ]
        )

        result = await run_pr_review(
            session, repo_name, _pr_info(), _diff_hunks(), llm=llm
        )
        # 1 out of 2 findings survived → pass_rate = 0.5
        # 0.9 * 0.5 = 0.45
        assert result.confidence < 0.5


# ---------------------------------------------------------------------------
# File cap
# ---------------------------------------------------------------------------
class TestPRReviewFileCap:
    async def test_huge_pr_truncated(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """A PR with 50 changed files gets capped to max_files."""
        session, repo_name = indexed_synth_py
        hunks = [
            DiffHunk(
                file_path=f"src/file_{i}.py",
                status="modified",
                additions=i,
                deletions=0,
                patch=f"@@ -1,1 +1,{i} @@\n+line",
                changed_ranges=[ChangedLineRange(start=1, count=i)],
            )
            for i in range(50)
        ]
        llm = FakeLLMClient(
            responses=[
                _findings_response(findings=[]),
                _summary_response(verdict="approve"),
            ]
        )

        result = await run_pr_review(
            session,
            repo_name,
            _pr_info(),
            hunks,
            llm=llm,
            max_files=5,
        )

        # The review completed without error — truncation worked.
        assert result.verdict == "approve"
        # The LLM prompt should have contained at most 5 files.
        review_call = llm.calls[0]
        # Count how many "=== src/file_" markers appear in the user prompt.
        file_markers = review_call["user"].count("=== src/file_")
        assert file_markers <= 5


# ---------------------------------------------------------------------------
# No github import
# ---------------------------------------------------------------------------
class TestNoGithubIO:
    def test_recipe_does_not_import_github_execute(self):
        """The recipe must not depend on GithubClient.execute — that's
        the bridge's job. It only takes PRInfo + DiffHunks as input."""
        import inspect

        from gita.agents.pr_reviewer import recipe

        source = inspect.getsource(recipe)
        assert "client.execute" not in source
        assert "GithubClient" not in source.replace("PRInfo", "")
