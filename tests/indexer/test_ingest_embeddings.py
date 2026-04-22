"""Embedding population during ingest.

Uses the ``FakeEmbeddingClient`` so no API calls are made. Verifies:
- full index populates the ``embedding`` column for every non-empty file
- incremental index only embeds changed files (unchanged rows keep
  whatever embedding they already had)
- passing ``None`` leaves ``embedding`` NULL everywhere (keyword FTS
  fallback contract)
- empty content files are skipped
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex, Repo
from gita.indexer.embeddings import (
    EMBEDDING_DIMS,
    EMBEDDING_INPUT_CHAR_LIMIT,
    FakeEmbeddingClient,
    prepare_embedding_input,
)
from gita.indexer.ingest import index_repository


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    """Minimal git repo with three Python files."""
    repo = tmp_path / "project"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")  # empty file
    (src / "core.py").write_text(
        "def main():\n    return 1\n"
    )
    (src / "utils.py").write_text(
        "def helper():\n    return 2\n"
    )

    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "."],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        capture_output=True, check=True,
    )
    return repo


def _commit(repo: Path, msg: str = "update") -> None:
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", msg],
        capture_output=True, check=True,
    )


async def _rows(session: AsyncSession) -> list[CodeIndex]:
    return (await session.execute(select(CodeIndex))).scalars().all()


async def _non_null_embedding_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count(CodeIndex.id)).where(
                CodeIndex.embedding.is_not(None)
            )
        )
    ).scalar_one()


class TestFullIndexEmbedding:
    async def test_populates_embeddings_for_nonempty_files(
        self, db_session: AsyncSession, py_repo: Path
    ):
        client = FakeEmbeddingClient()
        result = await index_repository(
            db_session, "test", py_repo, embedding_client=client
        )
        await db_session.commit()

        # core.py, utils.py → two non-empty files embedded.
        # __init__.py is empty → skipped.
        assert result.files_embedded == 2
        assert await _non_null_embedding_count(db_session) == 2

    async def test_embedding_dimensions_match_column(
        self, db_session: AsyncSession, py_repo: Path
    ):
        await index_repository(
            db_session,
            "test",
            py_repo,
            embedding_client=FakeEmbeddingClient(),
        )
        await db_session.commit()

        row = (
            await db_session.execute(
                select(CodeIndex).where(CodeIndex.file_path == "src/core.py")
            )
        ).scalar_one()
        # pgvector returns either a list or a numpy array; len() works on both.
        assert row.embedding is not None
        assert len(row.embedding) == EMBEDDING_DIMS

    async def test_empty_file_has_null_embedding(
        self, db_session: AsyncSession, py_repo: Path
    ):
        await index_repository(
            db_session,
            "test",
            py_repo,
            embedding_client=FakeEmbeddingClient(),
        )
        await db_session.commit()

        row = (
            await db_session.execute(
                select(CodeIndex).where(
                    CodeIndex.file_path == "src/__init__.py"
                )
            )
        ).scalar_one()
        assert row.embedding is None

    async def test_none_client_leaves_embeddings_null(
        self, db_session: AsyncSession, py_repo: Path
    ):
        result = await index_repository(
            db_session, "test", py_repo, embedding_client=None
        )
        await db_session.commit()

        assert result.files_embedded == 0
        assert await _non_null_embedding_count(db_session) == 0

    async def test_client_called_once_per_full_index(
        self, db_session: AsyncSession, py_repo: Path
    ):
        """Single batched embed call per ingest run, not one per file."""
        client = FakeEmbeddingClient()
        await index_repository(
            db_session, "test", py_repo, embedding_client=client
        )
        await db_session.commit()

        assert client.call_count == 1
        assert client.total_texts == 2  # core.py + utils.py


class TestIncrementalEmbedding:
    async def test_only_changed_files_are_embedded(
        self, db_session: AsyncSession, py_repo: Path
    ):
        # Full index with embeddings.
        client = FakeEmbeddingClient()
        await index_repository(
            db_session, "test", py_repo, embedding_client=client
        )
        await db_session.commit()
        assert client.total_texts == 2

        # Modify one file.
        (py_repo / "src" / "utils.py").write_text(
            "def helper():\n    return 99\n"
        )
        _commit(py_repo, "change helper")

        # Incremental index — should only embed utils.py.
        result = await index_repository(
            db_session, "test", py_repo, embedding_client=client
        )
        await db_session.commit()

        assert result.mode == "incremental"
        assert result.files_embedded == 1
        assert client.call_count == 2
        # 2 (initial) + 1 (incremental) = 3 total texts embedded.
        assert client.total_texts == 3

    async def test_unchanged_files_keep_existing_embedding(
        self, db_session: AsyncSession, py_repo: Path
    ):
        client = FakeEmbeddingClient()
        await index_repository(
            db_session, "test", py_repo, embedding_client=client
        )
        await db_session.commit()

        # Grab core.py's embedding before any modification.
        row_before = (
            await db_session.execute(
                select(CodeIndex).where(CodeIndex.file_path == "src/core.py")
            )
        ).scalar_one()
        assert row_before.embedding is not None
        emb_before = list(row_before.embedding)

        # Modify utils.py only.
        (py_repo / "src" / "utils.py").write_text(
            "def helper():\n    return 123\n"
        )
        _commit(py_repo)
        await index_repository(
            db_session, "test", py_repo, embedding_client=client
        )
        await db_session.commit()

        # core.py's embedding should be unchanged.
        row_after = (
            await db_session.execute(
                select(CodeIndex).where(CodeIndex.file_path == "src/core.py")
            )
        ).scalar_one()
        assert row_after.embedding is not None
        assert list(row_after.embedding) == pytest.approx(emb_before)


class TestPrepareEmbeddingInput:
    def test_empty_returns_empty(self):
        assert prepare_embedding_input("") == ""
        assert prepare_embedding_input(None) == ""

    def test_short_passes_through(self):
        assert prepare_embedding_input("hello") == "hello"

    def test_long_truncated(self):
        huge = "x" * (EMBEDDING_INPUT_CHAR_LIMIT + 500)
        out = prepare_embedding_input(huge)
        assert len(out) == EMBEDDING_INPUT_CHAR_LIMIT
        assert out == "x" * EMBEDDING_INPUT_CHAR_LIMIT
