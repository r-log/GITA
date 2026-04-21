"""Tests for the reindex runner.

Uses the test DB for repo resolution and monkeypatch for git operations.
The runner creates its own ``SessionLocal`` sessions, so we patch it
to use the test DB engine instead of the production one.
"""
from __future__ import annotations

import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex, Repo
from gita.indexer.ingest import index_repository
from gita.jobs.runners import _git_sync

SYNTH_REPO = (
    Path(__file__).parent.parent / "fixtures" / "synthetic_py"
).resolve()


@pytest_asyncio.fixture
async def _patch_runner_session(db_session: AsyncSession, monkeypatch):
    """Patch ``SessionLocal`` in runners.py so it yields the test session.

    The runner calls ``async with SessionLocal() as session:`` which
    normally connects to the production DB. We redirect it to the test
    DB session so the TRUNCATE cleanup in conftest covers runner writes.
    """

    @asynccontextmanager
    async def _fake_session_local():
        yield db_session

    monkeypatch.setattr(
        "gita.jobs.runners.SessionLocal", _fake_session_local
    )


# ---------------------------------------------------------------------------
# _git_sync unit tests (mock subprocess)
# ---------------------------------------------------------------------------
class TestGitSync:
    def test_success(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, err = _git_sync(Path("/tmp/repo"), "abc123")
        assert ok is True
        assert err == ""
        assert len(calls) == 2
        assert "fetch" in calls[0]
        assert "abc123" in calls[1]

    def test_success_without_sha_uses_origin_head(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, _ = _git_sync(Path("/tmp/repo"), None)
        assert ok is True
        assert "origin/HEAD" in calls[1]

    def test_fetch_failure(self, monkeypatch):
        def fake_run(cmd, **kw):
            if "fetch" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="fatal")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, err = _git_sync(Path("/tmp/repo"), "abc123")
        assert ok is False
        assert "git fetch failed" in err

    def test_reset_failure(self, monkeypatch):
        def fake_run(cmd, **kw):
            if "reset" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="fatal")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, err = _git_sync(Path("/tmp/repo"), "abc123")
        assert ok is False
        assert "git reset failed" in err

    def test_timeout(self, monkeypatch):
        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 120)

        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, err = _git_sync(Path("/tmp/repo"), "abc123")
        assert ok is False
        assert "git fetch failed" in err


# ---------------------------------------------------------------------------
# run_reindex_job: repo resolution + error paths
# ---------------------------------------------------------------------------
class TestReindexRunnerResolution:

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_repo_not_found(self, db_session: AsyncSession):
        """Repo not indexed -> returns error dict without git operations."""
        from gita.jobs.runners import run_reindex_job

        result = await run_reindex_job("r-log/nonexistent", after_sha="abc")
        assert result["status"] == "error"
        assert result["reason"] == "repo_not_indexed"

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_resolves_by_github_full_name(self, db_session: AsyncSession):
        """Repo indexed with github_full_name -> resolves correctly."""
        await index_repository(
            db_session,
            "synthetic_py",
            SYNTH_REPO,
            github_full_name="r-log/synthetic",
        )
        await db_session.flush()

        from gita.jobs.runners import run_reindex_job

        with patch(
            "gita.jobs.runners._git_sync", return_value=(True, "")
        ):
            result = await run_reindex_job(
                "r-log/synthetic", after_sha="abc123"
            )

        assert result["status"] == "completed"
        assert result["repo"] == "r-log/synthetic"

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_root_path_missing(self, db_session: AsyncSession):
        """Repo exists in DB but root_path directory is gone."""
        db_session.add(
            Repo(
                name="gone_repo",
                root_path="/tmp/nonexistent_dir_12345",
                github_full_name="r-log/gone",
            )
        )
        await db_session.flush()

        from gita.jobs.runners import run_reindex_job

        result = await run_reindex_job("r-log/gone")
        assert result["status"] == "error"
        assert result["reason"] == "root_path_missing"


class TestReindexRunnerGitFailure:

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_git_sync_failure_returns_error(self, db_session: AsyncSession):
        await index_repository(
            db_session,
            "synthetic_py",
            SYNTH_REPO,
            github_full_name="r-log/synthetic",
        )
        await db_session.flush()

        from gita.jobs.runners import run_reindex_job

        with patch(
            "gita.jobs.runners._git_sync",
            return_value=(False, "git fetch failed: timeout"),
        ):
            result = await run_reindex_job(
                "r-log/synthetic", after_sha="abc123"
            )

        assert result["status"] == "error"
        assert result["reason"] == "git_sync_failed"
        assert "timeout" in result["detail"]


class TestReindexRunnerSuccess:

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_completed_summary_shape(self, db_session: AsyncSession):
        await index_repository(
            db_session,
            "synthetic_py",
            SYNTH_REPO,
            github_full_name="r-log/synthetic",
        )
        await db_session.flush()

        from gita.jobs.runners import run_reindex_job

        with patch(
            "gita.jobs.runners._git_sync", return_value=(True, "")
        ):
            result = await run_reindex_job(
                "r-log/synthetic", after_sha="abc123"
            )

        assert result["status"] == "completed"
        assert "mode" in result
        assert "files_indexed" in result
        assert "files_deleted" in result
        assert "edges_total" in result
        assert result["after_sha"] == "abc123"

    @pytest.mark.usefixtures("_patch_runner_session")
    async def test_reindex_updates_code_index(self, db_session: AsyncSession):
        """After reindex, code_index rows should still exist."""
        await index_repository(
            db_session,
            "synthetic_py",
            SYNTH_REPO,
            github_full_name="r-log/synthetic",
        )
        await db_session.flush()

        from gita.jobs.runners import run_reindex_job

        with patch(
            "gita.jobs.runners._git_sync", return_value=(True, "")
        ):
            await run_reindex_job("r-log/synthetic", after_sha="abc123")

        rows = (await db_session.execute(select(CodeIndex))).scalars().all()
        assert len(rows) > 0
