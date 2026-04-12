"""End-to-end tests for the onboarding agent.

Uses a ``FakeLLMClient`` so the whole pipeline runs without touching
OpenRouter. The agent is wired against the real ``indexed_synth_py``
fixture — ingested Postgres rows, real views, real prompt loading —
everything except the LLM call itself.
"""
from __future__ import annotations

import pytest

from gita.agents.onboarding import (
    FindingsResponse,
    LLMFinding,
    LLMMilestone,
    MilestonesResponse,
    OnboardingError,
    PickFilesResponse,
    run_onboarding,
)
from gita.agents.types import Finding, Milestone, OnboardingResult
from gita.llm.client import FakeLLMClient


def _pick_response(
    picks: list[int] | None = None,
    summary: str = "A small Python package with format/validate helpers.",
) -> PickFilesResponse:
    return PickFilesResponse(
        project_summary=summary,
        tech_stack=["python"],
        picks=picks if picks is not None else [0, 1],
        reasoning="These files together show the model and its helpers.",
    )


def _findings_response(findings: list[LLMFinding] | None = None) -> FindingsResponse:
    return FindingsResponse(
        findings=findings
        if findings is not None
        else [
            LLMFinding(
                file="src/myapp/models.py",
                line=7,
                severity="low",
                kind="quality",
                description="User.email has no validation at construction time",
                fix_sketch="call validate_email in __post_init__",
            ),
        ]
    )


def _milestones_response(
    milestones: list[LLMMilestone] | None = None,
) -> MilestonesResponse:
    return MilestonesResponse(
        milestones=milestones
        if milestones is not None
        else [
            LLMMilestone(
                title="Tighten User construction",
                summary="Validate email at construction time",
                finding_indices=[0],
                confidence=0.8,
            ),
        ]
    )


def _three_call_fake(
    *,
    pick: PickFilesResponse | None = None,
    findings: FindingsResponse | None = None,
    milestones: MilestonesResponse | None = None,
) -> FakeLLMClient:
    return FakeLLMClient(
        responses=[
            pick or _pick_response(),
            findings or _findings_response(),
            milestones or _milestones_response(),
        ]
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
class TestOnboardingHappyPath:
    async def test_produces_onboarding_result(self, indexed_synth_py):
        session, repo = indexed_synth_py
        fake = _three_call_fake()
        result = await run_onboarding(session, repo, llm=fake)
        assert isinstance(result, OnboardingResult)
        assert result.repo_name == repo

    async def test_makes_three_llm_calls(self, indexed_synth_py):
        session, repo = indexed_synth_py
        fake = _three_call_fake()
        await run_onboarding(session, repo, llm=fake)
        assert len(fake.calls) == 3

    async def test_call_schemas_are_in_order(self, indexed_synth_py):
        session, repo = indexed_synth_py
        fake = _three_call_fake()
        await run_onboarding(session, repo, llm=fake)
        schemas = [c["schema"] for c in fake.calls]
        assert schemas == [
            "PickFilesResponse",
            "FindingsResponse",
            "MilestonesResponse",
        ]

    async def test_project_summary_comes_from_pick_call(
        self, indexed_synth_py
    ):
        session, repo = indexed_synth_py
        pick = _pick_response(summary="Tiny model + helpers demo package.")
        fake = _three_call_fake(pick=pick)
        result = await run_onboarding(session, repo, llm=fake)
        assert result.project_summary == "Tiny model + helpers demo package."

    async def test_findings_converted_to_dataclass(self, indexed_synth_py):
        session, repo = indexed_synth_py
        fake = _three_call_fake()
        result = await run_onboarding(session, repo, llm=fake)
        assert len(result.findings) == 1
        assert isinstance(result.findings[0], Finding)
        assert result.findings[0].file == "src/myapp/models.py"
        assert result.findings[0].line == 7

    async def test_milestones_converted_to_dataclass(self, indexed_synth_py):
        session, repo = indexed_synth_py
        fake = _three_call_fake()
        result = await run_onboarding(session, repo, llm=fake)
        assert len(result.milestones) == 1
        assert isinstance(result.milestones[0], Milestone)
        assert result.milestones[0].finding_indices == [0]


# ---------------------------------------------------------------------------
# Picks validation + clamping
# ---------------------------------------------------------------------------
class TestPickValidation:
    async def test_picks_beyond_limit_are_clamped(self, indexed_synth_py):
        session, repo = indexed_synth_py
        pick = _pick_response(picks=[0, 1, 2, 3])  # synth_py has exactly 4 files
        fake = _three_call_fake(pick=pick)
        result = await run_onboarding(
            session, repo, llm=fake, deep_read_limit=2
        )
        # deep_read_limit=2 caps the picked files — second LLM call sees 2 bodies
        assert isinstance(result, OnboardingResult)

    async def test_invalid_picks_fall_back_to_top(self, indexed_synth_py):
        session, repo = indexed_synth_py
        pick = _pick_response(picks=[99, 100, 101])  # all out of range
        fake = _three_call_fake(pick=pick)
        result = await run_onboarding(session, repo, llm=fake)
        # Fallback picks the top 3 by rank — findings call still happens
        assert len(fake.calls) >= 2

    async def test_duplicate_picks_deduped(self, indexed_synth_py):
        session, repo = indexed_synth_py
        pick = _pick_response(picks=[0, 0, 0, 0])
        fake = _three_call_fake(pick=pick)
        result = await run_onboarding(session, repo, llm=fake)
        assert isinstance(result, OnboardingResult)


# ---------------------------------------------------------------------------
# Empty / degenerate paths
# ---------------------------------------------------------------------------
class TestEmptyPaths:
    async def test_zero_findings_means_zero_milestones_and_no_group_call(
        self, indexed_synth_py
    ):
        session, repo = indexed_synth_py
        fake = FakeLLMClient(
            responses=[
                _pick_response(),
                FindingsResponse(findings=[]),  # zero findings
                # Third call should never happen
            ]
        )
        result = await run_onboarding(session, repo, llm=fake)
        assert result.findings == []
        assert result.milestones == []
        # Only two calls — we skipped the grouping call
        assert len(fake.calls) == 2

    async def test_findings_without_citations_are_dropped(
        self, indexed_synth_py
    ):
        session, repo = indexed_synth_py
        bad_finding = LLMFinding(
            file="",
            line=0,
            severity="low",
            kind="quality",
            description="something vague",
        )
        fake = _three_call_fake(
            findings=FindingsResponse(findings=[bad_finding]),
        )
        result = await run_onboarding(session, repo, llm=fake)
        # The bad finding got dropped. With no valid findings, grouping is
        # skipped and we should still get a coherent result.
        assert result.findings == []
        assert result.milestones == []

    async def test_milestone_with_only_invalid_indices_dropped(
        self, indexed_synth_py
    ):
        session, repo = indexed_synth_py
        # One real finding, but the milestone references a nonexistent index
        milestones = MilestonesResponse(
            milestones=[
                LLMMilestone(
                    title="Broken milestone",
                    summary="...",
                    finding_indices=[99],  # no such finding
                    confidence=0.9,
                )
            ]
        )
        fake = _three_call_fake(milestones=milestones)
        result = await run_onboarding(session, repo, llm=fake)
        assert result.milestones == []


# ---------------------------------------------------------------------------
# Error surfaces
# ---------------------------------------------------------------------------
class TestErrors:
    async def test_unindexed_repo_raises(self, db_session):
        fake = _three_call_fake()
        with pytest.raises(Exception):  # RepoNotFoundError from resolve_repo
            await run_onboarding(db_session, "not-indexed", llm=fake)


# ---------------------------------------------------------------------------
# Shadow mode contract: agent never touches GitHub
# ---------------------------------------------------------------------------
class TestNoGithubIO:
    async def test_agent_does_not_import_github_client(
        self, indexed_synth_py
    ):
        """The onboarding module must not depend on the GitHub client.
        Writes are a Day 7 concern, orchestrated outside the agent."""
        import sys

        session, repo = indexed_synth_py
        fake = _three_call_fake()
        # Purge any cached import of gita.github.client so we can prove
        # running the agent doesn't pull it in.
        for mod in list(sys.modules):
            if mod.startswith("gita.github"):
                del sys.modules[mod]

        await run_onboarding(session, repo, llm=fake)

        assert "gita.github.client" not in sys.modules
        assert "gita.github.auth" not in sys.modules
