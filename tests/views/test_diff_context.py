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
    SymbolInDiff,
    _overlaps,
    _symbols_near_changes,
    context_windows,
    count_lines,
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
class TestDiffContextViewWithoutHead:
    """No head content (e.g. fetch failed): base copy as unnumbered context."""

    async def test_indexed_file_falls_back_to_base_copy(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        hunks = [_hunk("src/myapp/utils.py", ranges=[(1, 3)])]

        result = await diff_context_view(session, repo_name, hunks)

        assert isinstance(result, DiffContextResult)
        assert result.total_count == 1 and result.indexed_count == 1
        ctx = result.files[0]
        assert ctx.indexed is True
        assert ctx.language == "python"
        assert ctx.head_content is None
        assert "format_name" in ctx.base_content
        # Base-side symbols would be in the wrong coordinates; none given.
        assert ctx.symbols_near_changes == []
        # Only the diff's own new-side lines are citable.
        assert ctx.visible_lines() == {1, 2, 3}

    async def test_imported_by_populated(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """utils.py is imported by core.py and models.py — high impact."""
        session, repo_name = indexed_synth_py
        hunks = [_hunk("src/myapp/utils.py", ranges=[(1, 2)])]

        result = await diff_context_view(session, repo_name, hunks)

        assert {"src/myapp/core.py", "src/myapp/models.py"} <= set(
            result.files[0].imported_by
        )

    async def test_unindexed_added_file(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        hunks = [_hunk("src/brand_new_file.py", status="added", ranges=[(1, 10)])]

        result = await diff_context_view(session, repo_name, hunks)

        ctx = result.files[0]
        assert result.indexed_count == 0
        assert ctx.indexed is False
        assert ctx.base_content is None and ctx.head_content is None
        assert ctx.imported_by == []


_HEAD_UTILS = (
    "def format_name(name: str) -> str:\n"
    "    if not name.strip():\n"
    "        return ''\n"
    "    return name.strip().title()\n"
    "\n"
    "\n"
    "def validate_email(email: str) -> bool:\n"
    "    return '@' in email\n"
)


class TestDiffContextViewWithHead:
    async def test_head_content_is_numbered_context_and_symbols(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        hunks = [_hunk("src/myapp/utils.py", ranges=[(2, 2)])]

        result = await diff_context_view(
            session, repo_name, hunks, head_contents={"src/myapp/utils.py": _HEAD_UTILS}
        )

        ctx = result.files[0]
        assert ctx.head_content == _HEAD_UTILS
        assert ctx.head_line_count == 8
        assert ctx.context_windows == [(1, 8)]  # short file: shown whole
        assert ctx.visible_lines() == set(range(1, 9))
        # Symbols come from the head version, in head coordinates.
        names = {s.name: (s.start_line, s.end_line) for s in ctx.symbols_near_changes}
        assert names == {"format_name": (1, 4)}

    async def test_file_added_by_the_pr_gets_full_context(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        head = "def charge(q):\n    return db.execute(f'SELECT {q}')\n"
        hunks = [_hunk("src/myapp/payments.py", status="added", ranges=[(1, 2)])]

        result = await diff_context_view(
            session, repo_name, hunks, head_contents={"src/myapp/payments.py": head}
        )

        ctx = result.files[0]
        assert ctx.indexed is False
        assert ctx.visible_lines() == {1, 2}
        assert [s.name for s in ctx.symbols_near_changes] == ["charge"]


class TestContextWindows:
    def test_short_file_shown_whole(self):
        assert context_windows(120, [ChangedLineRange(50, 3)]) == [(1, 120)]

    def test_long_file_windows_merge(self):
        ranges = [ChangedLineRange(100, 5), ChangedLineRange(140, 1), ChangedLineRange(900, 2)]
        assert context_windows(1000, ranges) == [(70, 170), (870, 931)]

    def test_windows_clamped_to_file(self):
        assert context_windows(400, [ChangedLineRange(395, 6)]) == [(365, 400)]

    def test_empty_file(self):
        assert context_windows(0, []) == []


def test_count_lines_matches_git():
    assert count_lines("") == 0
    assert count_lines("a") == 1
    assert count_lines("a\n") == 1
    assert count_lines("a\nb") == 2
    # Form feed and U+2028 are not line breaks for git/GitHub.
    assert count_lines("a\fb\u2028c\n") == 1


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
