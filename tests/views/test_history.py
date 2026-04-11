"""history_view tests — build a real tiny git repo in tmp_path, index it,
then exercise the view against it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio

from gita.indexer.ingest import index_repository
from gita.views._common import RepoNotFoundError
from gita.views.history import HistoryResult, history_view


GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git not available")


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _make_commit(
    repo: Path, file_rel: str, content: str, author_name: str, author_email: str, message: str
) -> None:
    target = repo / file_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(["add", file_rel], cwd=repo)
    _git(
        [
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "-m",
            message,
        ],
        cwd=repo,
    )


@pytest_asyncio.fixture
async def tmp_git_repo(tmp_path, db_session):
    """Create a tiny git repo with 3 commits by 2 different authors, then
    index it into the DB. Yields (session, repo_name)."""
    repo_root = tmp_path / "tmp_repo"
    repo_root.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=repo_root)

    _make_commit(
        repo_root,
        "app.py",
        "def one():\n    return 1\n",
        "Alice",
        "alice@example.com",
        "initial commit",
    )
    _make_commit(
        repo_root,
        "app.py",
        "def one():\n    return 1\n\ndef two():\n    return 2\n",
        "Bob",
        "bob@example.com",
        "add two",
    )
    _make_commit(
        repo_root,
        "app.py",
        "def one():\n    return 1\n\ndef two():\n    return 2\n\ndef three():\n    return 3\n",
        "Alice",
        "alice@example.com",
        "add three",
    )

    await index_repository(db_session, "tmp_repo", repo_root)
    await db_session.commit()
    yield db_session, "tmp_repo", "app.py"


class TestHistoryView:
    async def test_returns_result(self, tmp_git_repo):
        session, repo, file_path = tmp_git_repo
        result = await history_view(session, repo, file_path)
        assert isinstance(result, HistoryResult)
        assert result.file_path == file_path
        assert result.git_available is True

    async def test_recent_commits_count(self, tmp_git_repo):
        session, repo, file_path = tmp_git_repo
        result = await history_view(session, repo, file_path)
        # 3 commits to app.py
        assert len(result.recent_commits) == 3

    async def test_most_recent_commit_first(self, tmp_git_repo):
        session, repo, file_path = tmp_git_repo
        result = await history_view(session, repo, file_path)
        # git log default order is newest first
        assert result.recent_commits[0].message == "add three"
        assert result.recent_commits[-1].message == "initial commit"

    async def test_commit_fields_populated(self, tmp_git_repo):
        session, repo, file_path = tmp_git_repo
        result = await history_view(session, repo, file_path)
        commit = result.recent_commits[0]
        assert len(commit.sha) == 40
        assert len(commit.short_sha) >= 7
        assert commit.author in ("Alice", "Bob")
        # ISO 8601 date — very rough check
        assert "T" in commit.date or "-" in commit.date
        assert commit.message

    async def test_blame_summary_totals(self, tmp_git_repo):
        session, repo, file_path = tmp_git_repo
        result = await history_view(session, repo, file_path)
        # Final file has 8 lines: 3 functions × 2 lines each + 2 blank lines
        # separating them. Exact count depends on how git attributes them.
        total = sum(result.blame_summary.values())
        assert total > 0
        # Both authors should appear in the blame summary
        assert set(result.blame_summary.keys()) <= {"Alice", "Bob"}
        assert "Alice" in result.blame_summary
        assert "Bob" in result.blame_summary


class TestHistoryErrors:
    async def test_unknown_repo_raises(self, db_session):
        with pytest.raises(RepoNotFoundError):
            await history_view(db_session, "no-such-repo", "file.py")

    async def test_missing_root_path_returns_empty(
        self, tmp_path, db_session, tmp_git_repo
    ):
        """If the indexed repo's root_path has disappeared, return an empty
        result with git_available=False rather than crashing."""
        session, repo_name, file_path = tmp_git_repo
        # Point repo to a nonexistent dir
        from sqlalchemy import update
        from gita.db.models import Repo

        await session.execute(
            update(Repo)
            .where(Repo.name == repo_name)
            .values(root_path=str(tmp_path / "does-not-exist"))
        )
        await session.commit()

        result = await history_view(session, repo_name, file_path)
        assert result.git_available is False
        assert result.recent_commits == []
