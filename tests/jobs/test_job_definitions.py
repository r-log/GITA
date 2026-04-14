"""Tests for ARQ job definitions.

Verifies that job functions are registered, accept the expected kwargs,
and that ``ALL_JOBS`` is complete. ``review_pr`` and ``onboard_repo``
delegate to runners that need external services — those are tested in
``test_runners.py``. Here we only test the ``reindex_repo`` stub and
the registry.
"""
from __future__ import annotations

import pytest

from gita.jobs import ALL_JOBS, onboard_repo, reindex_repo, review_pr


@pytest.fixture
def ctx() -> dict:
    """Fake ARQ context dict (normally has redis, job_id, etc.)."""
    return {"redis": None}


class TestReindexRepo:
    async def test_returns_dict(self, ctx):
        result = await reindex_repo(
            ctx, repo_full_name="r-log/AMASS", after_sha="def456"
        )
        assert isinstance(result, dict)
        assert result["repo"] == "r-log/AMASS"
        assert result["after_sha"] == "def456"

    async def test_after_sha_optional(self, ctx):
        result = await reindex_repo(ctx, repo_full_name="r-log/AMASS")
        assert result["after_sha"] is None


class TestAllJobs:
    def test_all_jobs_complete(self):
        """ALL_JOBS must include every public job function."""
        assert review_pr in ALL_JOBS
        assert onboard_repo in ALL_JOBS
        assert reindex_repo in ALL_JOBS

    def test_no_unexpected_jobs(self):
        assert len(ALL_JOBS) == 3

    def test_job_functions_are_coroutines(self):
        """ARQ requires async functions."""
        import asyncio

        assert asyncio.iscoroutinefunction(review_pr)
        assert asyncio.iscoroutinefunction(onboard_repo)
        assert asyncio.iscoroutinefunction(reindex_repo)
