"""Tests for load_bearing_view against the synthetic_py fixture.

synthetic_py's import topology:
    utils.py     — no imports, imported by models.py AND core.py
    models.py    — imports utils.py (+ stdlib dataclasses), imported by core.py
    core.py      — imports models.py + utils.py (+ stdlib os), imported by nothing
    __init__.py  — no imports, imported by nothing

Expected in-degrees:
    utils.py     = 2
    models.py    = 1
    core.py      = 0
    __init__.py  = 0
"""
from __future__ import annotations

import pytest

from gita.views._common import RepoNotFoundError
from gita.views.load_bearing import (
    LoadBearingResult,
    RankedFile,
    load_bearing_view,
)


class TestLoadBearingRanking:
    async def test_returns_result(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo)
        assert isinstance(result, LoadBearingResult)
        assert result.repo_name == repo
        assert result.limit == 10
        assert result.total_files == 4

    async def test_four_files_returned_under_default_limit(
        self, indexed_synth_py
    ):
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo)
        assert len(result.files) == 4

    async def test_utils_is_first_with_in_degree_2(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo)
        top = result.files[0]
        assert top.file_path == "src/myapp/utils.py"
        assert top.in_degree == 2

    async def test_models_is_second_with_in_degree_1(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo)
        second = result.files[1]
        assert second.file_path == "src/myapp/models.py"
        assert second.in_degree == 1

    async def test_core_and_init_have_zero_in_degree(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo)
        zero_files = [f for f in result.files if f.in_degree == 0]
        names = {f.file_path for f in zero_files}
        assert names == {"src/myapp/__init__.py", "src/myapp/core.py"}

    async def test_ordering_is_stable(self, indexed_synth_py):
        """Same call should return identical ordering every time."""
        session, repo = indexed_synth_py
        first = await load_bearing_view(session, repo)
        second = await load_bearing_view(session, repo)
        assert [f.file_path for f in first.files] == [
            f.file_path for f in second.files
        ]

    async def test_tiebreak_is_alphabetical_file_path(self, indexed_synth_py):
        """core.py (0) should come before __init__.py (0) because of sort?
        Actually __init__.py sorts before core.py alphabetically.
        """
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo)
        zero_rank_order = [
            f.file_path for f in result.files if f.in_degree == 0
        ]
        assert zero_rank_order == [
            "src/myapp/__init__.py",
            "src/myapp/core.py",
        ]


class TestLoadBearingLimit:
    async def test_limit_two_returns_two(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo, limit=2)
        assert len(result.files) == 2
        assert result.limit == 2
        # Still the highest in-degree ones
        assert result.files[0].file_path == "src/myapp/utils.py"
        assert result.files[1].file_path == "src/myapp/models.py"

    async def test_limit_one_returns_top(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo, limit=1)
        assert len(result.files) == 1
        assert result.files[0].file_path == "src/myapp/utils.py"

    async def test_limit_huge_returns_all_files(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo, limit=1000)
        assert len(result.files) == 4

    async def test_limit_zero_raises(self, indexed_synth_py):
        session, repo = indexed_synth_py
        with pytest.raises(ValueError, match="limit must be positive"):
            await load_bearing_view(session, repo, limit=0)

    async def test_limit_negative_raises(self, indexed_synth_py):
        session, repo = indexed_synth_py
        with pytest.raises(ValueError, match="limit must be positive"):
            await load_bearing_view(session, repo, limit=-5)


class TestLoadBearingSymbolSummary:
    async def test_files_carry_symbol_summary(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo)
        utils = next(f for f in result.files if f.file_path.endswith("utils.py"))
        names = {s.name for s in utils.symbol_summary}
        assert {"format_name", "validate_email"}.issubset(names)

    async def test_models_summary_has_user_class(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo)
        models = next(
            f for f in result.files if f.file_path.endswith("models.py")
        )
        kinds_by_name = {s.name: s.kind for s in models.symbol_summary}
        assert kinds_by_name.get("User") == "class"

    async def test_symbol_summary_is_metadata_only(self, indexed_synth_py):
        """RankedFile.symbol_summary must not carry code bodies — enforces
        the 'views are navigation, symbol_view returns code' rule."""
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo)
        for f in result.files:
            for brief in f.symbol_summary:
                assert not hasattr(brief, "code")
                assert not hasattr(brief, "content")


class TestLoadBearingErrors:
    async def test_unknown_repo_raises(self, db_session):
        with pytest.raises(RepoNotFoundError):
            await load_bearing_view(db_session, "nope")


class TestRankedFileShape:
    async def test_ranked_file_has_expected_fields(self, indexed_synth_py):
        session, repo = indexed_synth_py
        result = await load_bearing_view(session, repo)
        f = result.files[0]
        assert isinstance(f, RankedFile)
        assert f.file_path
        assert f.language == "python"
        assert f.line_count > 0
        assert f.in_degree >= 0
        assert isinstance(f.symbol_summary, list)
