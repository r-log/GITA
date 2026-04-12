"""Tests for the diff parser — pure, no I/O.

Exercises ``parse_pr_files`` and ``_extract_changed_ranges`` against
fixture JSON matching GitHub's ``pulls/{n}/files`` response shape.
"""
from __future__ import annotations

from gita.agents.pr_reviewer.diff_parser import (
    ChangedLineRange,
    DiffHunk,
    _extract_changed_ranges,
    parse_pr_files,
)


# ---------------------------------------------------------------------------
# _extract_changed_ranges
# ---------------------------------------------------------------------------
class TestExtractChangedRanges:
    def test_single_hunk(self):
        patch = "@@ -10,5 +10,8 @@ def foo():\n context line"
        ranges = _extract_changed_ranges(patch)
        assert len(ranges) == 1
        assert ranges[0] == ChangedLineRange(start=10, count=8)

    def test_multiple_hunks(self):
        patch = (
            "@@ -1,3 +1,5 @@ header1\n"
            " line\n"
            "@@ -20,4 +22,7 @@ header2\n"
            " line"
        )
        ranges = _extract_changed_ranges(patch)
        assert len(ranges) == 2
        assert ranges[0] == ChangedLineRange(start=1, count=5)
        assert ranges[1] == ChangedLineRange(start=22, count=7)

    def test_single_line_hunk_no_count(self):
        """When the count is omitted (``+42`` instead of ``+42,3``),
        it means one line."""
        patch = "@@ -1 +42 @@ one liner"
        ranges = _extract_changed_ranges(patch)
        assert len(ranges) == 1
        assert ranges[0] == ChangedLineRange(start=42, count=1)

    def test_zero_count_deletion_only(self):
        """A hunk with ``+X,0`` means pure deletion — no new lines added."""
        patch = "@@ -10,3 +10,0 @@ deleted section"
        ranges = _extract_changed_ranges(patch)
        assert len(ranges) == 1
        assert ranges[0].count == 0
        assert ranges[0].start == 10

    def test_none_patch(self):
        assert _extract_changed_ranges(None) == []

    def test_empty_patch(self):
        assert _extract_changed_ranges("") == []

    def test_no_hunk_headers(self):
        assert _extract_changed_ranges("just some text") == []


class TestChangedLineRangeEnd:
    def test_end_property(self):
        r = ChangedLineRange(start=10, count=5)
        assert r.end == 14  # 10, 11, 12, 13, 14

    def test_single_line_end_equals_start(self):
        r = ChangedLineRange(start=42, count=1)
        assert r.end == 42

    def test_zero_count_end_equals_start(self):
        r = ChangedLineRange(start=10, count=0)
        assert r.end == 10


# ---------------------------------------------------------------------------
# parse_pr_files
# ---------------------------------------------------------------------------
class TestParsePrFiles:
    def test_basic_parsing(self):
        raw = [
            {
                "sha": "abc",
                "filename": "src/db.py",
                "status": "modified",
                "additions": 10,
                "deletions": 3,
                "patch": "@@ -40,7 +40,14 @@ def get_user():\n-old\n+new",
            },
            {
                "sha": "def",
                "filename": "src/new_file.py",
                "status": "added",
                "additions": 50,
                "deletions": 0,
                "patch": "@@ -0,0 +1,50 @@\n+new content",
            },
        ]
        hunks = parse_pr_files(raw)
        assert len(hunks) == 2
        assert all(isinstance(h, DiffHunk) for h in hunks)

        assert hunks[0].file_path == "src/db.py"
        assert hunks[0].status == "modified"
        assert hunks[0].additions == 10
        assert hunks[0].deletions == 3
        assert len(hunks[0].changed_ranges) == 1
        assert hunks[0].changed_ranges[0].start == 40

        assert hunks[1].file_path == "src/new_file.py"
        assert hunks[1].status == "added"
        assert hunks[1].changed_ranges[0].start == 1
        assert hunks[1].changed_ranges[0].count == 50

    def test_binary_file_no_patch(self):
        raw = [
            {
                "filename": "logo.png",
                "status": "added",
                "additions": 0,
                "deletions": 0,
                # no "patch" key — binary file
            },
        ]
        hunks = parse_pr_files(raw)
        assert len(hunks) == 1
        assert hunks[0].patch is None
        assert hunks[0].changed_ranges == []

    def test_renamed_file(self):
        raw = [
            {
                "filename": "src/new_name.py",
                "previous_filename": "src/old_name.py",
                "status": "renamed",
                "additions": 0,
                "deletions": 0,
                "patch": "",
            },
        ]
        hunks = parse_pr_files(raw)
        assert hunks[0].previous_filename == "src/old_name.py"
        assert hunks[0].status == "renamed"

    def test_deleted_file(self):
        raw = [
            {
                "filename": "src/dead.py",
                "status": "removed",
                "additions": 0,
                "deletions": 30,
                "patch": "@@ -1,30 +0,0 @@\n-all deleted",
            },
        ]
        hunks = parse_pr_files(raw)
        assert hunks[0].status == "removed"
        assert hunks[0].deletions == 30
        assert hunks[0].changed_ranges[0].count == 0

    def test_empty_list(self):
        assert parse_pr_files([]) == []

    def test_preserves_order(self):
        raw = [
            {"filename": "z.py", "status": "modified", "additions": 1, "deletions": 0},
            {"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0},
        ]
        hunks = parse_pr_files(raw)
        assert [h.file_path for h in hunks] == ["z.py", "a.py"]

    def test_multiple_hunks_in_one_file(self):
        raw = [
            {
                "filename": "src/big.py",
                "status": "modified",
                "additions": 20,
                "deletions": 5,
                "patch": (
                    "@@ -10,5 +10,8 @@ first hunk\n ctx\n"
                    "@@ -100,3 +103,10 @@ second hunk\n ctx"
                ),
            },
        ]
        hunks = parse_pr_files(raw)
        assert len(hunks[0].changed_ranges) == 2
        assert hunks[0].changed_ranges[0].start == 10
        assert hunks[0].changed_ranges[1].start == 103
