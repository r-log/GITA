"""Diff parsing — turn GitHub's PR files response into structured hunks.

Pure module: no I/O, no GitHub client dependency. Takes the JSON shape
from ``GET /repos/{owner}/{repo}/pulls/{n}/files`` and produces
``DiffHunk`` dataclasses that the diff context view and the reviewer
recipe consume.

Separated from the GitHub client so tests can exercise the parser with
fixture JSON (no HTTP) and future sources (git CLI, GitLab) can reuse it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ChangedLineRange:
    """A contiguous range of changed lines within a single hunk header.

    ``start`` is 1-based, ``count`` is the number of lines. A range with
    ``count=0`` means the hunk is a pure deletion (no new lines added).
    """

    start: int
    count: int

    @property
    def end(self) -> int:
        """Inclusive end line (or ``start`` when count is 0)."""
        return self.start + max(self.count - 1, 0)


@dataclass
class DiffHunk:
    """One file's contribution to a PR diff.

    ``patch`` is the raw unified diff text from GitHub (may be ``None``
    for binary files or files too large for GitHub to return a patch).
    ``changed_ranges`` lists the new-side line ranges extracted from
    ``@@ ... @@`` hunk headers — these are the lines the PR reviewer
    should focus on.
    """

    file_path: str
    status: str  # "added" | "modified" | "removed" | "renamed" | "copied"
    additions: int
    deletions: int
    patch: str | None
    changed_ranges: list[ChangedLineRange] = field(default_factory=list)
    previous_filename: str | None = None  # set for renames


# Regex for unified-diff hunk headers: @@ -old_start[,old_count] +new_start[,new_count] @@
_HUNK_HEADER_RE = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@",
    re.MULTILINE,
)


def _extract_changed_ranges(patch: str | None) -> list[ChangedLineRange]:
    """Extract new-side line ranges from unified diff hunk headers.

    Each ``@@ -X,Y +A,B @@`` header contributes one ``ChangedLineRange``
    with ``start=A, count=B``. When the count is omitted (single-line
    hunk) it defaults to 1.
    """
    if not patch:
        return []
    ranges: list[ChangedLineRange] = []
    for match in _HUNK_HEADER_RE.finditer(patch):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        ranges.append(ChangedLineRange(start=start, count=count))
    return ranges


def parse_pr_files(pr_files_json: list[dict]) -> list[DiffHunk]:
    """Parse GitHub's ``pulls/{n}/files`` response into ``DiffHunk`` list.

    Expects the raw JSON array from the GitHub API. Each element has at
    minimum ``filename``, ``status``, ``additions``, ``deletions``; the
    ``patch`` field may be missing for binary or oversized files.

    The returned list preserves the API's ordering (typically alphabetical
    by file path).
    """
    hunks: list[DiffHunk] = []
    for entry in pr_files_json:
        patch = entry.get("patch")
        hunks.append(
            DiffHunk(
                file_path=entry["filename"],
                status=entry.get("status", "modified"),
                additions=entry.get("additions", 0),
                deletions=entry.get("deletions", 0),
                patch=patch,
                changed_ranges=_extract_changed_ranges(patch),
                previous_filename=entry.get("previous_filename"),
            )
        )
    return hunks
