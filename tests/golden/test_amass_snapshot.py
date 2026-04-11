"""Golden snapshot test against a local clone of r-log/AMASS.

This is the Day 6 feel-test in automated form. We run the real ingest
pipeline against a real repo and assert aggregate invariants — NOT exact
counts, because AMASS moves. Counts have a tolerance so normal development
doesn't break the test; a catastrophic regression still will.

**Skip behavior:** if the AMASS checkout isn't at the expected path on this
machine, the whole module is skipped. Set the ``GITA_AMASS_PATH`` env var
to point somewhere else if your layout differs.

To update the snapshot intentionally after you know a change is legitimate:
adjust the constants at the top of this file.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from gita.indexer.ingest import index_repository
from gita.views.neighborhood import neighborhood_view
from gita.views.symbol import symbol_view

_DEFAULT_AMASS_PATH = Path(
    "C:/Users/Roko/Documents/PYTHON/AMAS/electrician-log-mvp"
)
AMASS_PATH = Path(os.environ.get("GITA_AMASS_PATH", str(_DEFAULT_AMASS_PATH)))

pytestmark = pytest.mark.skipif(
    not AMASS_PATH.is_dir(),
    reason=f"AMASS not available at {AMASS_PATH}",
)


# Count invariants — adjust these intentionally when AMASS legitimately grows
# or shrinks. Every one is a floor or a range, not an exact match.
MIN_FILES = 60
MIN_PYTHON_FILES = 40
MIN_JS_FILES = 15
MIN_FUNCTIONS = 500
MIN_CLASSES = 20
MIN_IMPORT_EDGES = 200


class TestAmassIngest:
    async def test_indexes_cleanly(self, db_session):
        result = await index_repository(db_session, "amass", AMASS_PATH)
        await db_session.commit()

        assert result.files_indexed >= MIN_FILES, (
            f"expected ≥{MIN_FILES} files, got {result.files_indexed}"
        )
        assert result.functions_extracted >= MIN_FUNCTIONS
        assert result.classes_extracted >= MIN_CLASSES
        assert result.edges_total >= MIN_IMPORT_EDGES

    async def test_head_sha_captured(self, db_session):
        result = await index_repository(db_session, "amass", AMASS_PATH)
        await db_session.commit()
        assert result.head_sha is not None
        assert len(result.head_sha) == 40

    async def test_both_languages_present(self, db_session):
        from sqlalchemy import select

        from gita.db.models import CodeIndex, Repo

        await index_repository(db_session, "amass", AMASS_PATH)
        await db_session.commit()

        repo = (
            await db_session.execute(select(Repo).where(Repo.name == "amass"))
        ).scalar_one()

        rows = (
            await db_session.execute(
                select(CodeIndex).where(CodeIndex.repo_id == repo.id)
            )
        ).scalars().all()

        by_lang: dict[str, int] = {}
        for row in rows:
            by_lang[row.language] = by_lang.get(row.language, 0) + 1

        assert by_lang.get("python", 0) >= MIN_PYTHON_FILES
        assert by_lang.get("javascript", 0) >= MIN_JS_FILES

    async def test_no_parser_crashes(self, db_session):
        """Every row must have a well-formed structure dict."""
        from sqlalchemy import select

        from gita.db.models import CodeIndex, Repo

        await index_repository(db_session, "amass", AMASS_PATH)
        await db_session.commit()

        repo = (
            await db_session.execute(select(Repo).where(Repo.name == "amass"))
        ).scalar_one()

        rows = (
            await db_session.execute(
                select(CodeIndex).where(CodeIndex.repo_id == repo.id)
            )
        ).scalars().all()

        for row in rows:
            assert row.structure is not None
            assert set(row.structure.keys()) == {
                "functions",
                "classes",
                "imports",
            }
            assert row.line_count > 0 or row.content == ""


class TestAmassViews:
    async def test_symbol_view_finds_authmanager(self, db_session):
        await index_repository(db_session, "amass", AMASS_PATH)
        await db_session.commit()

        result = await symbol_view(db_session, "amass", "AuthManager")
        assert result.total_matches >= 1
        top = result.matches[0]
        assert top.kind == "class"
        assert top.name == "AuthManager"
        assert "class AuthManager" in top.code
        assert "frontend/auth.js" in top.file_path

    async def test_neighborhood_view_on_auth_js(self, db_session):
        await index_repository(db_session, "amass", AMASS_PATH)
        await db_session.commit()

        result = await neighborhood_view(db_session, "amass", "frontend/auth.js")
        assert result.file.file_path == "frontend/auth.js"
        assert result.file.language == "javascript"
        # AuthManager has ~20 methods; the file should have at least 10 symbols
        assert len(result.file.symbol_summary) >= 10
        # AuthManager class should be in the summary
        names = {s.name for s in result.file.symbol_summary}
        assert "AuthManager" in names
