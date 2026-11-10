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

SYNTH_REPO = (Path(__file__).parent.parent / "fixtures" / "synthetic_py").resolve()


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

    monkeypatch.setattr("gita.jobs.runners.SessionLocal", _fake_session_local)


# ---------------------------------------------------------------------------
# _git_sync against real git repositories
# ---------------------------------------------------------------------------
def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """An upstream repo and a clone of it (the directory GITA indexes)."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "a.py", "x = 1\n")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))
    return upstream, clone


class TestGitSync:
    def test_checks_out_the_pushed_commit_detached(self, origin_and_clone):
        upstream, clone = origin_and_clone
        local_main_before = _git(clone, "rev-parse", "main")
        new_sha = _commit(upstream, "b.py", "y = 2\n")

        ok, err = _git_sync(clone, new_sha)

        assert ok, err
        assert _git(clone, "rev-parse", "HEAD") == new_sha
        assert (clone / "b.py").exists()
        # No branch pointer was moved (reset --hard used to move main).
        assert _git(clone, "rev-parse", "main") == local_main_before
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"

    def test_refuses_to_discard_uncommitted_work(self, origin_and_clone):
        upstream, clone = origin_and_clone
        new_sha = _commit(upstream, "b.py", "y = 2\n")
        (clone / "a.py").write_text("x = 'my unsaved edit'\n")

        ok, err = _git_sync(clone, new_sha)

        assert ok is False
        assert "uncommitted changes" in err
        assert (clone / "a.py").read_text() == "x = 'my unsaved edit'\n"

    def test_untracked_files_do_not_block_or_get_deleted(self, origin_and_clone):
        upstream, clone = origin_and_clone
        new_sha = _commit(upstream, "b.py", "y = 2\n")
        (clone / "notes.txt").write_text("scratch")

        ok, err = _git_sync(clone, new_sha)

        assert ok, err
        assert (clone / "notes.txt").read_text() == "scratch"

    def test_unknown_commit_fails_cleanly(self, origin_and_clone):
        _, clone = origin_and_clone
        ok, err = _git_sync(clone, "f" * 40)
        assert ok is False
        assert "git checkout failed" in err

    def test_fetch_failure(self, origin_and_clone, tmp_path):
        _, clone = origin_and_clone
        _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone"))
        ok, err = _git_sync(clone, "f" * 40)
        assert ok is False
        assert "git fetch failed" in err

    def test_timeout_is_reported(self, monkeypatch):
        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 120)

        monkeypatch.setattr(subprocess, "run", fake_run)
        ok, err = _git_sync(Path("/tmp/repo"), "abc123")
        assert ok is False
        assert "timed out" in err


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

        with patch("gita.jobs.runners._git_sync", return_value=(True, "")):
            result = await run_reindex_job("r-log/synthetic", after_sha="abc123")

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
            result = await run_reindex_job("r-log/synthetic", after_sha="abc123")

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

        with patch("gita.jobs.runners._git_sync", return_value=(True, "")):
            result = await run_reindex_job("r-log/synthetic", after_sha="abc123")

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

        with patch("gita.jobs.runners._git_sync", return_value=(True, "")):
            await run_reindex_job("r-log/synthetic", after_sha="abc123")

        rows = (await db_session.execute(select(CodeIndex))).scalars().all()
        assert len(rows) > 0
