"""End-to-end ingest test against tests/fixtures/synthetic_py.

Runs the real ingest pipeline against a real (tiny) repo structure, hits
Postgres, and verifies every row shape.
"""
from pathlib import Path

import pytest
from sqlalchemy import select

from gita.db.models import CodeIndex, ImportEdge, Repo
from gita.indexer.ingest import index_repository

SYNTH_REPO = (
    Path(__file__).parent.parent / "fixtures" / "synthetic_py"
).resolve()


@pytest.fixture
def synth_root() -> Path:
    assert SYNTH_REPO.is_dir(), f"synthetic_py fixture missing at {SYNTH_REPO}"
    return SYNTH_REPO


class TestIngestSyntheticPy:
    async def test_repo_row_created(self, db_session, synth_root):
        result = await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        repos = (await db_session.execute(select(Repo))).scalars().all()
        assert len(repos) == 1
        repo = repos[0]
        assert repo.name == "synthetic_py"
        assert repo.root_path == str(synth_root)
        assert repo.indexed_at is not None
        assert str(repo.id) == result.repo_id

    async def test_file_count_and_identities(self, db_session, synth_root):
        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        rows = (await db_session.execute(select(CodeIndex))).scalars().all()
        paths = {row.file_path for row in rows}
        assert paths == {
            "src/myapp/__init__.py",
            "src/myapp/utils.py",
            "src/myapp/models.py",
            "src/myapp/core.py",
            "pyproject.toml",
        } - {"pyproject.toml"}  # pyproject is NOT a source file, walker skips it
        # The assertion above is deliberately spelled out so the intent is
        # obvious: only .py files land in code_index.
        assert len(rows) == 4

    async def test_tests_directory_excluded(self, db_session, synth_root):
        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        rows = (await db_session.execute(select(CodeIndex))).scalars().all()
        for row in rows:
            assert "tests/" not in row.file_path
            assert row.file_path != "tests/test_core.py"

    async def test_every_row_has_language_python(self, db_session, synth_root):
        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        rows = (await db_session.execute(select(CodeIndex))).scalars().all()
        assert all(r.language == "python" for r in rows)

    async def test_content_stored(self, db_session, synth_root):
        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        row = (
            await db_session.execute(
                select(CodeIndex).where(CodeIndex.file_path == "src/myapp/utils.py")
            )
        ).scalar_one()
        assert "def format_name" in row.content
        assert "def validate_email" in row.content
        assert row.line_count > 0

    async def test_structure_jsonb_shape(self, db_session, synth_root):
        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        row = (
            await db_session.execute(
                select(CodeIndex).where(CodeIndex.file_path == "src/myapp/models.py")
            )
        ).scalar_one()

        structure = row.structure
        assert set(structure.keys()) == {"functions", "classes", "imports"}
        class_names = {c["name"] for c in structure["classes"]}
        assert "User" in class_names

        # User has two methods
        method_names = {
            f["name"]
            for f in structure["functions"]
            if f["kind"] in ("method", "async_method") and f["parent_class"] == "User"
        }
        assert method_names == {"display_name", "has_valid_email"}

    async def test_import_edges_total_count(self, db_session, synth_root):
        result = await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        # utils.py: 0 imports
        # __init__.py: 0 imports
        # models.py: 2 imports (dataclasses, .utils)
        # core.py: 3 imports (os, .models, .utils)
        # Total: 5 edges
        assert result.edges_total == 5

        edges = (await db_session.execute(select(ImportEdge))).scalars().all()
        assert len(edges) == 5

    async def test_relative_imports_resolved(self, db_session, synth_root):
        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        # core.py imports .models → should resolve to src/myapp/models.py
        edge = (
            await db_session.execute(
                select(ImportEdge)
                .where(ImportEdge.src_file == "src/myapp/core.py")
                .where(ImportEdge.raw_import.contains(".models"))
            )
        ).scalar_one()
        assert edge.dst_file == "src/myapp/models.py"

        # models.py imports .utils → should resolve to src/myapp/utils.py
        edge = (
            await db_session.execute(
                select(ImportEdge)
                .where(ImportEdge.src_file == "src/myapp/models.py")
                .where(ImportEdge.raw_import.contains(".utils"))
            )
        ).scalar_one()
        assert edge.dst_file == "src/myapp/utils.py"

    async def test_stdlib_imports_unresolved(self, db_session, synth_root):
        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        # os and dataclasses are stdlib → dst_file should be NULL
        edges = (
            (
                await db_session.execute(
                    select(ImportEdge).where(ImportEdge.dst_file.is_(None))
                )
            )
            .scalars()
            .all()
        )
        raws = {e.raw_import for e in edges}
        assert any("os" in r for r in raws)
        assert any("dataclasses" in r for r in raws)

    async def test_edges_resolved_count(self, db_session, synth_root):
        result = await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        # Resolved: core→models, core→utils, models→utils = 3
        assert result.edges_resolved == 3

    async def test_result_aggregates(self, db_session, synth_root):
        result = await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        assert result.files_indexed == 4
        assert result.functions_extracted > 0
        assert result.classes_extracted >= 1  # User


class TestGithubFullName:
    """Tests for the github_full_name parameter on index_repository."""

    async def test_github_full_name_stored_on_create(self, db_session, synth_root):
        await index_repository(
            db_session, "synthetic_py", synth_root, github_full_name="r-log/synthetic"
        )
        await db_session.commit()

        repo = (await db_session.execute(select(Repo))).scalar_one()
        assert repo.github_full_name == "r-log/synthetic"

    async def test_github_full_name_backfilled_on_reindex(self, db_session, synth_root):
        """If a repo was indexed without github_full_name, a later re-index
        with the flag set should backfill the column."""
        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        repo = (await db_session.execute(select(Repo))).scalar_one()
        assert repo.github_full_name is None

        await index_repository(
            db_session, "synthetic_py", synth_root, github_full_name="r-log/synthetic"
        )
        await db_session.commit()

        await db_session.refresh(repo)
        assert repo.github_full_name == "r-log/synthetic"

    async def test_github_full_name_not_overwritten(self, db_session, synth_root):
        """Once set, github_full_name isn't replaced by a None re-index."""
        await index_repository(
            db_session, "synthetic_py", synth_root, github_full_name="r-log/synthetic"
        )
        await db_session.commit()

        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        repo = (await db_session.execute(select(Repo))).scalar_one()
        assert repo.github_full_name == "r-log/synthetic"

    async def test_github_full_name_optional(self, db_session, synth_root):
        """Default behaviour: no github_full_name, column stays NULL."""
        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()

        repo = (await db_session.execute(select(Repo))).scalar_one()
        assert repo.github_full_name is None


class TestReindex:
    async def test_reindex_is_idempotent(self, db_session, synth_root):
        """Running index_repository twice should leave the same row counts."""
        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()
        first_files = (
            await db_session.execute(select(CodeIndex))
        ).scalars().all()
        first_edges = (
            await db_session.execute(select(ImportEdge))
        ).scalars().all()

        await index_repository(db_session, "synthetic_py", synth_root)
        await db_session.commit()
        second_files = (
            await db_session.execute(select(CodeIndex))
        ).scalars().all()
        second_edges = (
            await db_session.execute(select(ImportEdge))
        ).scalars().all()

        assert len(first_files) == len(second_files)
        assert len(first_edges) == len(second_edges)
        # Still only one repo row
        repos = (await db_session.execute(select(Repo))).scalars().all()
        assert len(repos) == 1
