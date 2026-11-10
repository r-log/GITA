"""run_pr_review_job: skip paths that must not spend LLM tokens.

Reviews are debounced (see ``gita.web.dispatch``): every push enqueues a
review for its commit, deferred a few seconds. When a job runs, it checks
the PR's live head; if a newer commit has landed, the job for that commit
is already queued, so this one skips. Same for PRs closed in the meantime.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.types import PRReviewResult
from gita.github.client import PRInfo
from gita.indexer.ingest import index_repository

SYNTH_REPO = (Path(__file__).parent.parent / "fixtures" / "synthetic_py").resolve()
OLD_SHA, NEW_SHA = "1" * 40, "2" * 40


@pytest_asyncio.fixture
async def runner_env(db_session: AsyncSession, monkeypatch):
    await index_repository(
        db_session, "synthetic_py", SYNTH_REPO, github_full_name="r-log/synthetic"
    )
    await db_session.flush()

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr("gita.jobs.runners.SessionLocal", _session)
    monkeypatch.setattr("gita.jobs.runners.settings.openrouter_api_key", "fake")
    monkeypatch.setattr("gita.jobs.runners.settings.github_app_id", 123)
    monkeypatch.setattr("gita.jobs.runners.settings.github_app_private_key_path", "/fake")


def _pr(head_sha: str, state: str = "open") -> PRInfo:
    return PRInfo(
        number=7,
        title="t",
        body="",
        author="dev",
        state=state,
        base_ref="main",
        head_ref="feature",
        head_sha=head_sha,
        changed_files=1,
        additions=1,
        deletions=0,
        html_url="",
    )


async def _run_job(pr: PRInfo, job_sha: str):
    from gita.jobs.runners import run_pr_review_job

    gh = AsyncMock()
    gh.get_pr = AsyncMock(return_value=pr)
    gh.get_pr_files = AsyncMock(return_value=[])
    gh.__aenter__ = AsyncMock(return_value=gh)
    gh.__aexit__ = AsyncMock(return_value=None)
    review = AsyncMock(
        return_value=PRReviewResult(
            repo_name="synthetic_py",
            pr_number=7,
            pr_title="t",
            summary="ok",
            verdict="approve",
            findings=[],
            confidence=0.9,
        )
    )
    with (
        patch("gita.jobs.runners.GithubAppAuth.from_files", return_value=AsyncMock()),
        patch("gita.jobs.runners.GithubClient", return_value=gh),
        patch("gita.jobs.runners.run_pr_review", review),
        patch("gita.jobs.runners.OpenRouterClient") as llm_cls,
    ):
        result = await run_pr_review_job("r-log/synthetic", 7, head_sha=job_sha)
    return result, review, llm_cls


@pytest.mark.usefixtures("runner_env")
class TestSkipBeforeLLM:
    async def test_superseded_commit_is_skipped(self):
        result, review, llm_cls = await _run_job(_pr(head_sha=NEW_SHA), job_sha=OLD_SHA)
        assert result["status"] == "skipped"
        assert result["reason"] == "superseded"
        assert result["current_head_sha"] == NEW_SHA
        review.assert_not_called()
        llm_cls.assert_not_called()

    @pytest.mark.parametrize("state", ["closed", "merged"])
    async def test_closed_pr_is_skipped(self, state):
        result, review, _ = await _run_job(_pr(head_sha=OLD_SHA, state=state), job_sha=OLD_SHA)
        assert result["status"] == "skipped"
        assert result["reason"] == f"pr_{state}"
        review.assert_not_called()

    async def test_current_commit_is_reviewed(self, monkeypatch):
        monkeypatch.setattr("gita.jobs.runners.settings.write_mode", "shadow")
        result, review, _ = await _run_job(_pr(head_sha=OLD_SHA), job_sha=OLD_SHA)
        review.assert_called_once()
        assert result["status"] == "completed"
        assert result["head_sha"] == OLD_SHA
