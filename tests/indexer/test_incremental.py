"""Integration tests for incremental re-indexing.

Uses a real tmpdir git repo with controlled commits to test the full
index → modify → incremental index pipeline. Each test gets its own
DB session via the ``db_session`` fixture so there's no cross-talk.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex, ImportEdge, Repo
from gita.indexer.ingest import index_repository


# ---------------------------------------------------------------------------
# Fixture: a real git repo with Python files
# ---------------------------------------------------------------------------
@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    """Create a git repo with 3 Python files and one commit."""
    repo = tmp_path / "myproject"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")
    (src / "core.py").write_text(
        "from src.utils import helper\n\n"
        "def main():\n    return helper()\n"
    )
    (src / "utils.py").write_text(
        "def helper():\n    return 42\n"
    )
    (src / "models.py").write_text(
        "class User:\n    pass\n"
    )

    subprocess.run(
        ["git", "init", str(repo)], capture_output=True, check=True
    )
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


def _commit(repo: Path, message: str = "update") -> None:
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message,
         "--allow-empty"],
        capture_output=True, check=True,
    )


async def _file_count(session: AsyncSession, repo_name: str) -> int:
    repo = (
        await session.execute(select(Repo).where(Repo.name == repo_name))
    ).scalar_one()
    return (
        await session.execute(
            select(func.count(CodeIndex.id)).where(
                CodeIndex.repo_id == repo.id
            )
        )
    ).scalar_one()


async def _get_file_content(
    session: AsyncSession, repo_name: str, file_path: str
) -> str | None:
    repo = (
        await session.execute(select(Repo).where(Repo.name == repo_name))
    ).scalar_one()
    row = (
        await session.execute(
            select(CodeIndex)
            .where(CodeIndex.repo_id == repo.id)
            .where(CodeIndex.file_path == file_path)
        )
    ).scalar_one_or_none()
    return row.content if row else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestFullIndex:
    async def test_first_index_is_full(
        self, db_session: AsyncSession, py_repo: Path
    ):
        result = await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        assert result.mode == "full"
        assert result.files_indexed >= 3  # core, utils, models (+ __init__)

    async def test_force_full_always_full(
        self, db_session: AsyncSession, py_repo: Path
    ):
        # First index
        await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        # Second index with --full
        result = await index_repository(
            db_session, "test", py_repo, force_full=True
        )
        await db_session.commit()

        assert result.mode == "full"


class TestNoop:
    async def test_no_changes_returns_noop(
        self, db_session: AsyncSession, py_repo: Path
    ):
        await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        # Re-index without any changes
        result = await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        assert result.mode == "noop"
        assert result.files_indexed == 0


class TestIncrementalModify:
    async def test_modified_file_re_parsed(
        self, db_session: AsyncSession, py_repo: Path
    ):
        # Full index
        await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        old_content = await _get_file_content(
            db_session, "test", "src/utils.py"
        )
        assert old_content is not None
        assert "42" in old_content

        # Modify utils.py and commit
        (py_repo / "src" / "utils.py").write_text(
            "def helper():\n    return 99\n"
        )
        _commit(py_repo, "change helper return value")

        # Incremental index
        result = await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        assert result.mode == "incremental"
        assert result.files_indexed == 1  # only utils.py re-parsed

        new_content = await _get_file_content(
            db_session, "test", "src/utils.py"
        )
        assert new_content is not None
        assert "99" in new_content
        assert "42" not in new_content

    async def test_unmodified_files_untouched(
        self, db_session: AsyncSession, py_repo: Path
    ):
        """Files NOT in the diff should keep their existing rows."""
        await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        original_count = await _file_count(db_session, "test")

        # Modify only utils.py
        (py_repo / "src" / "utils.py").write_text(
            "def helper():\n    return 99\n"
        )
        _commit(py_repo)

        await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        # Total file count should be unchanged.
        assert await _file_count(db_session, "test") == original_count


class TestIncrementalAdd:
    async def test_new_file_added(
        self, db_session: AsyncSession, py_repo: Path
    ):
        await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        original_count = await _file_count(db_session, "test")

        # Add a new file
        (py_repo / "src" / "new_module.py").write_text(
            "def new_func():\n    return 'new'\n"
        )
        _commit(py_repo, "add new module")

        result = await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        assert result.mode == "incremental"
        assert result.files_indexed == 1  # only new_module.py
        assert await _file_count(db_session, "test") == original_count + 1

        content = await _get_file_content(
            db_session, "test", "src/new_module.py"
        )
        assert content is not None
        assert "new_func" in content


class TestIncrementalDelete:
    async def test_deleted_file_removed(
        self, db_session: AsyncSession, py_repo: Path
    ):
        await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        original_count = await _file_count(db_session, "test")
        assert await _get_file_content(
            db_session, "test", "src/models.py"
        ) is not None

        # Delete models.py
        (py_repo / "src" / "models.py").unlink()
        _commit(py_repo, "remove models")

        result = await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        assert result.mode == "incremental"
        assert result.files_deleted == 1
        assert await _file_count(db_session, "test") == original_count - 1
        assert await _get_file_content(
            db_session, "test", "src/models.py"
        ) is None


class TestIncrementalEdges:
    async def test_import_edges_rebuilt_for_changed_file(
        self, db_session: AsyncSession, py_repo: Path
    ):
        """When a file's imports change, its old edges should be replaced."""
        await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        repo = (
            await db_session.execute(
                select(Repo).where(Repo.name == "test")
            )
        ).scalar_one()

        # core.py currently imports utils. Change it to import models.
        (py_repo / "src" / "core.py").write_text(
            "from src.models import User\n\n"
            "def main():\n    return User()\n"
        )
        _commit(py_repo, "change core imports")

        await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        # Check core.py's outgoing edges.
        edges = (
            await db_session.execute(
                select(ImportEdge)
                .where(ImportEdge.repo_id == repo.id)
                .where(ImportEdge.src_file == "src/core.py")
            )
        ).scalars().all()

        raw_imports = [e.raw_import for e in edges]
        assert any("models" in r for r in raw_imports)
        # The old "utils" import should be gone.
        assert not any("utils" in r and "models" not in r for r in raw_imports)


class TestNonSourceFilesSkipped:
    async def test_readme_change_doesnt_create_row(
        self, db_session: AsyncSession, py_repo: Path
    ):
        await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        original_count = await _file_count(db_session, "test")

        # Add a README (not a source file)
        (py_repo / "README.md").write_text("# My Project\n")
        _commit(py_repo, "add readme")

        result = await index_repository(db_session, "test", py_repo)
        await db_session.commit()

        assert result.mode == "incremental"
        assert result.files_indexed == 0  # README is not a source file
        assert await _file_count(db_session, "test") == original_count
