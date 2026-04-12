"""Tests for diff_context_view.

Uses the ``indexed_synth_py`` fixture which indexes the synthetic_py
fixture into the test DB. DiffHunks are constructed by hand to simulate
PR changes against known files, so we can assert symbol overlap and
reverse-dep lookups against ground truth.

Synthetic_py layout:
- src/myapp/utils.py    — format_name (L1-2), validate_email (L5-6)
- src/myapp/models.py   — User class (L6-15), display_name (L11-12), has_valid_email (L14-15)
- src/myapp/core.py     — create_user (L7-8), main (L11-14)
- src/myapp/__init__.py — empty

Import graph:
  core.py → models.py, utils.py
  models.py → utils.py
  → utils.py has in_degree=2 (imported by core + models)
  → models.py has in_degree=1 (imported by core)
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.pr_reviewer.diff_parser import ChangedLineRange, DiffHunk
from gita.views.diff_context import (
    DiffContextResult,
    FileContext,
    SymbolInDiff,
    _overlaps,
    _symbols_near_changes,
    diff_context_view,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _hunk(
    file_path: str,
    status: str = "modified",
    ranges: list[tuple[int, int]] | None = None,
    patch: str | None = "@@ -1,5 +1,5 @@\n ctx",
) -> DiffHunk:
    changed = [
        ChangedLineRange(start=s, count=c) for s, c in (ranges or [])
    ]
    return DiffHunk(
        file_path=file_path,
        status=status,
        additions=5,
        deletions=2,
        patch=patch,
        changed_ranges=changed,
    )


# ---------------------------------------------------------------------------
# Pure overlap helpers
# ---------------------------------------------------------------------------
class TestOverlaps:
    def test_symbol_inside_range(self):
        sym = SymbolInDiff("f", "function", start_line=10, end_line=15)
        ranges = [ChangedLineRange(start=8, count=10)]
        assert _overlaps(sym, ranges) is True

    def test_symbol_overlaps_start(self):
        sym = SymbolInDiff("f", "function", start_line=5, end_line=12)
        ranges = [ChangedLineRange(start=10, count=5)]
        assert _overlaps(sym, ranges) is True

    def test_symbol_overlaps_end(self):
        sym = SymbolInDiff("f", "function", start_line=13, end_line=20)
        ranges = [ChangedLineRange(start=10, count=5)]  # end=14
        assert _overlaps(sym, ranges) is True

    def test_symbol_before_range(self):
        sym = SymbolInDiff("f", "function", start_line=1, end_line=5)
        ranges = [ChangedLineRange(start=10, count=5)]
        assert _overlaps(sym, ranges) is False

    def test_symbol_after_range(self):
        sym = SymbolInDiff("f", "function", start_line=20, end_line=25)
        ranges = [ChangedLineRange(start=10, count=5)]
        assert _overlaps(sym, ranges) is False

    def test_exact_boundary(self):
        sym = SymbolInDiff("f", "function", start_line=14, end_line=20)
        ranges = [ChangedLineRange(start=10, count=5)]  # end=14
        assert _overlaps(sym, ranges) is True

    def test_empty_ranges(self):
        sym = SymbolInDiff("f", "function", start_line=1, end_line=10)
        assert _overlaps(sym, []) is False


class TestSymbolsNearChanges:
    def test_finds_overlapping_symbols(self):
        structure = {
            "functions": [
                {"name": "foo", "kind": "function", "start_line": 1, "end_line": 5},
                {"name": "bar", "kind": "function", "start_line": 10, "end_line": 20},
            ],
            "classes": [],
        }
        # Change hits lines 3-7 → overlaps foo (1-5) but not bar (10-20)
        ranges = [ChangedLineRange(start=3, count=5)]
        result = _symbols_near_changes(structure, ranges)
        assert len(result) == 1
        assert result[0].name == "foo"

    def test_empty_structure(self):
        assert _symbols_near_changes({}, [ChangedLineRange(1, 5)]) == []

    def test_empty_ranges(self):
        structure = {
            "functions": [
                {"name": "foo", "kind": "function", "start_line": 1, "end_line": 5},
            ],
            "classes": [],
        }
        assert _symbols_near_changes(structure, []) == []


# ---------------------------------------------------------------------------
# DB-integrated: diff_context_view against indexed_synth_py
# ---------------------------------------------------------------------------
class TestDiffContextViewIndexed:
    async def test_indexed_file_returns_context(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        hunks = [_hunk("src/myapp/utils.py", ranges=[(1, 3)])]

        result = await diff_context_view(session, repo_name, hunks)

        assert isinstance(result, DiffContextResult)
        assert result.total_count == 1
        assert result.indexed_count == 1
        assert len(result.files) == 1

        ctx = result.files[0]
        assert ctx.indexed is True
        assert ctx.language == "python"
        assert ctx.content is not None
        assert "format_name" in ctx.content

    async def test_symbols_near_changes_found(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """A diff touching lines 1-3 of utils.py should overlap format_name
        (starts at line 1) but not validate_email (starts at line 5)."""
        session, repo_name = indexed_synth_py
        hunks = [_hunk("src/myapp/utils.py", ranges=[(1, 2)])]

        result = await diff_context_view(session, repo_name, hunks)
        ctx = result.files[0]

        symbol_names = [s.name for s in ctx.symbols_near_changes]
        assert "format_name" in symbol_names
        # validate_email is at lines 5-6, should NOT overlap lines 1-2
        assert "validate_email" not in symbol_names

    async def test_imported_by_populated(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """utils.py is imported by core.py and models.py — those should
        appear in imported_by so the agent knows the change is high-impact."""
        session, repo_name = indexed_synth_py
        hunks = [_hunk("src/myapp/utils.py", ranges=[(1, 2)])]

        result = await diff_context_view(session, repo_name, hunks)
        ctx = result.files[0]

        assert len(ctx.imported_by) >= 2
        paths = set(ctx.imported_by)
        assert "src/myapp/core.py" in paths
        assert "src/myapp/models.py" in paths

    async def test_all_symbols_populated(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """all_symbols should include every symbol in the file, not just
        those near changes — the agent uses this for full-file context."""
        session, repo_name = indexed_synth_py
        hunks = [_hunk("src/myapp/utils.py", ranges=[(1, 1)])]

        result = await diff_context_view(session, repo_name, hunks)
        ctx = result.files[0]

        all_names = [s.name for s in ctx.all_symbols]
        assert "format_name" in all_names
        assert "validate_email" in all_names


class TestDiffContextViewUnindexed:
    async def test_unindexed_file_graceful_fallback(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        hunks = [_hunk("src/brand_new_file.py", ranges=[(1, 10)])]

        result = await diff_context_view(session, repo_name, hunks)

        assert result.total_count == 1
        assert result.indexed_count == 0
        ctx = result.files[0]
        assert ctx.indexed is False
        assert ctx.content is None
        assert ctx.symbols_near_changes == []
        assert ctx.imported_by == []


class TestDiffContextViewMixed:
    async def test_mixed_indexed_and_unindexed(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """A PR that modifies an indexed file and adds a new one."""
        session, repo_name = indexed_synth_py
        hunks = [
            _hunk("src/myapp/core.py", ranges=[(7, 3)]),
            _hunk("src/new_module.py", status="added", ranges=[(1, 50)]),
        ]

        result = await diff_context_view(session, repo_name, hunks)

        assert result.total_count == 2
        assert result.indexed_count == 1
        assert result.files[0].indexed is True
        assert result.files[1].indexed is False


class TestDiffContextViewEmpty:
    async def test_empty_hunks(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        result = await diff_context_view(session, repo_name, [])
        assert result.total_count == 0
        assert result.files == []
