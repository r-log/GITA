"""Tests for early repo resolution in all three runners.

Verifies that:
1. Runners resolve repos by ``github_full_name`` (webhook path).
2. Unindexed repos return an error dict without calling LLM or GitHub API.
3. The canonical short name is used for downstream calls.

Uses the test DB and patches ``SessionLocal`` in the runners module.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import Repo
from gita.indexer.ingest import index_repository

SYNTH_REPO = (
    Path(__file__).parent.parent / "fixtures" / "synthetic_py"
).resolve()


@pytest_asyncio.fixture
async def _patch_runner_session(db_session: AsyncSession, monkeypatch):
    """Redirect ``SessionLocal`` in runners to the test session."""

    @asynccontextmanager
    async def _fake():
        yield db_session

    monkeypatch.setattr("gita.jobs.runners.SessionLocal", _fake)


@pytest_asyncio.fixture
async def indexed_repo(db_session: AsyncSession):
    """Create an indexed repo with github_full_name set."""
    await index_repository(
        db_session,
        "synthetic_py",
        SYNTH_REPO,
        github_full_name="r-log/synthetic",
    )
    await db_session.flush()
    return "r-log/synthetic"


# ---------------------------------------------------------------------------
# PR review runner: early resolution
# ---------------------------------------------------------------------------
class TestPrReviewResolution:

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_not_indexed_returns_error_without_api_calls(
        self, db_session: AsyncSession
    ):
        """If the repo is not indexed, return error without GitHub/LLM calls."""
        from gita.jobs.runners import run_pr_review_job

        result = await run_pr_review_job("r-log/unknown", 1, head_sha="abc")
        assert result["status"] == "error"
        assert result["reason"] == "repo_not_indexed"

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_uses_canonical_short_name(
        self, db_session: AsyncSession, indexed_repo, monkeypatch
    ):
        """After resolution, the short name 'synthetic_py' is used downstream."""
        from gita.agents.types import PRReviewResult
        from gita.jobs.runners import run_pr_review_job

        # Patch credentials so we get past validation
        monkeypatch.setattr("gita.jobs.runners.settings.openrouter_api_key", "fake")
        monkeypatch.setattr("gita.jobs.runners.settings.github_app_id", "123")
        monkeypatch.setattr(
            "gita.jobs.runners.settings.github_app_private_key_path", "/fake"
        )

        # Mock GithubAppAuth and GithubClient
        mock_auth = AsyncMock()
        with patch("gita.jobs.runners.GithubAppAuth.from_files", return_value=mock_auth):
            mock_gh = AsyncMock()
            mock_gh.get_pr = AsyncMock(return_value=AsyncMock(head_sha="abc"))
            mock_gh.get_pr_files = AsyncMock(return_value=[])
            mock_gh.__aenter__ = AsyncMock(return_value=mock_gh)
            mock_gh.__aexit__ = AsyncMock(return_value=None)

            with patch("gita.jobs.runners.GithubClient", return_value=mock_gh):
                mock_llm = AsyncMock()
                mock_llm.__aenter__ = AsyncMock(return_value=mock_llm)
                mock_llm.__aexit__ = AsyncMock(return_value=None)

                with patch("gita.jobs.runners.OpenRouterClient", return_value=mock_llm):
                    captured = {}

                    async def fake_review(session, repo_name, pr_info, diff_hunks, *, llm):
                        captured["repo_name"] = repo_name
                        return PRReviewResult(
                            repo_name=repo_name,
                            pr_number=42,
                            pr_title="test",
                            summary="ok",
                            verdict="approve",
                            findings=[],
                            confidence=0.9,
                        )

                    with patch("gita.jobs.runners.run_pr_review", side_effect=fake_review):
                        await run_pr_review_job(indexed_repo, 42, head_sha="abc")

        # The canonical short name must be used, not the full GitHub name
        assert captured["repo_name"] == "synthetic_py"


# ---------------------------------------------------------------------------
# Onboarding runner: early resolution
# ---------------------------------------------------------------------------
class TestOnboardingResolution:

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_not_indexed_returns_error(self, db_session: AsyncSession):
        from gita.jobs.runners import run_onboarding_job

        result = await run_onboarding_job("r-log/unknown", 7)
        assert result["status"] == "error"
        assert result["reason"] == "repo_not_indexed"

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_resolves_by_github_full_name(
        self, db_session: AsyncSession, indexed_repo, monkeypatch
    ):
        """Repo found by full name — proceeds past resolution."""
        from gita.jobs.runners import run_onboarding_job

        # Force credentials to be missing so it raises after resolution
        monkeypatch.setattr("gita.jobs.runners.settings.openrouter_api_key", "")

        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            await run_onboarding_job(indexed_repo, 7)


# ---------------------------------------------------------------------------
# Reindex runner: early resolution
# ---------------------------------------------------------------------------
class TestReindexResolution:

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_not_indexed_returns_error(self, db_session: AsyncSession):
        from gita.jobs.runners import run_reindex_job

        result = await run_reindex_job("r-log/unknown")
        assert result["status"] == "error"
        assert result["reason"] == "repo_not_indexed"

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_resolves_by_github_full_name(
        self, db_session: AsyncSession, indexed_repo
    ):
        from gita.jobs.runners import run_reindex_job

        with patch("gita.jobs.runners._git_sync", return_value=(True, "")):
            result = await run_reindex_job(indexed_repo, after_sha="abc")
        assert result["status"] == "completed"
