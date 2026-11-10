"""Regression tests for incremental-indexing bugs, against real git repos."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex, ImportEdge, Repo
from gita.indexer.embeddings import FakeEmbeddingClient
from gita.indexer.ingest import index_repository
from gita.indexer.walker import MAX_FILE_SIZE


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t.com", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _commit_all(repo: Path, message: str = "update") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.py").write_text("import b\nimport c\n\ndef f():\n    return b.g()\n")
    (root / "b.py").write_text("def g():\n    return 1\n")
    _commit_all(root, "initial")
    return root


async def _paths(session: AsyncSession) -> set[str]:
    return set((await session.execute(select(CodeIndex.file_path))).scalars().all())


async def _edges(session: AsyncSession) -> set[tuple[str, str | None]]:
    rows = (await session.execute(select(ImportEdge.src_file, ImportEdge.dst_file))).all()
    return {(src, dst) for src, dst in rows}


class TestIncrementalRegressions:
    async def test_untracked_files_are_not_indexed(self, db_session, repo):
        (repo / "scratch.py").write_text("x = 1\n")  # never committed
        await index_repository(db_session, "r", repo)
        assert "scratch.py" not in await _paths(db_session)

    async def test_file_present_before_commit_does_not_wedge_reindex(self, db_session, repo):
        """Used to raise a unique violation on every subsequent reindex."""
        await index_repository(db_session, "r", repo)
        (repo / "new.py").write_text("y = 2\n")
        _git(repo, "add", "new.py")  # staged: indexed by a full run
        await index_repository(db_session, "r", repo, force_full=True)
        _commit_all(repo, "add new")  # now it arrives as "added"

        result = await index_repository(db_session, "r", repo)

        assert result.mode == "incremental"
        assert "new.py" in await _paths(db_session)

    async def test_import_edges_follow_deletes_and_adds(self, db_session, repo):
        await index_repository(db_session, "r", repo)
        assert await _edges(db_session) == {("a.py", "b.py"), ("a.py", None)}

        (repo / "b.py").unlink()
        (repo / "c.py").write_text("def h():\n    return 2\n")
        _commit_all(repo)
        await index_repository(db_session, "r", repo)

        # No edge points at the deleted b.py; "import c" now resolves.
        assert await _edges(db_session) == {("a.py", None), ("a.py", "c.py")}

    async def test_size_cap_applies_to_incremental_updates(self, db_session, repo):
        await index_repository(db_session, "r", repo)
        (repo / "big.py").write_text("x = 1\n" * (MAX_FILE_SIZE // 5))
        _commit_all(repo)
        await index_repository(db_session, "r", repo)
        assert "big.py" not in await _paths(db_session)

    async def test_non_ascii_paths_are_indexed(self, db_session, repo):
        await index_repository(db_session, "r", repo)
        (repo / "café.py").write_text("def crème():\n    return 1\n")
        _commit_all(repo)
        result = await index_repository(db_session, "r", repo)
        assert "café.py" in await _paths(db_session)
        assert result.added_files == ["café.py"]

    @pytest.mark.skipif(os.name != "posix", reason="symlinks")
    async def test_symlinks_are_never_followed(self, db_session, repo, tmp_path):
        secret = tmp_path / "host_secret.py"
        secret.write_text("TOKEN = 'ghs_should_never_be_indexed'\n")
        (repo / "link.py").symlink_to(secret)
        _commit_all(repo)

        await index_repository(db_session, "r", repo, force_full=True)
        contents = (await db_session.execute(select(CodeIndex.content))).scalars().all()

        assert "link.py" not in await _paths(db_session)
        assert not any("ghs_should_never" in (c or "") for c in contents)

    async def test_embedding_outage_does_not_abort_reindex(self, db_session, repo):
        class Down(FakeEmbeddingClient):
            async def embed(self, texts):
                raise RuntimeError("embeddings API 503")

        result = await index_repository(db_session, "r", repo, embedding_client=Down())

        assert result.files_indexed == 2 and result.files_embedded == 0
        repo_row = (await db_session.execute(select(Repo))).scalar_one()
        assert repo_row.head_sha is not None
