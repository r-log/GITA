"""Diff context view — what the PR reviewer shows the model for each file.

Given the PR's ``DiffHunk`` list and (optionally) each changed file's
content at the PR head, this view returns per file:

- the content the model sees, **in head coordinates** (the same line
  numbers as the diff's ``+`` side), either whole or as windows around
  the changed lines for long files;
- the exact set of line numbers the model was shown, so the guardrails
  can reject citations of lines it never saw;
- symbols overlapping the changed lines, parsed from the head content;
- reverse dependencies from the index (who imports this file).

When head content is unavailable (fetch failed, file too large, local
runs without GitHub), the view falls back to the indexed copy, which is
the *base* version. That copy is passed as unnumbered context only:
numbering it would invite citations in the wrong coordinate system.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.pr_reviewer.diff_parser import ChangedLineRange, DiffHunk
from gita.db.models import CodeIndex, ImportEdge
from gita.indexer.parsers import parse_file
from gita.indexer.walker import LANGUAGE_BY_EXT
from gita.views._common import resolve_repo

logger = logging.getLogger(__name__)

# Files up to this many lines are shown whole; longer ones as windows of
# ``_WINDOW_RADIUS`` lines around each changed range.
_WHOLE_FILE_MAX_LINES = 300
_WINDOW_RADIUS = 30


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class SymbolInDiff:
    """A symbol whose line range overlaps a changed region of the diff."""

    name: str
    kind: str  # "function" | "class" | "method" | ...
    start_line: int
    end_line: int
    parent_class: str | None = None


@dataclass
class FileContext:
    """What the reviewer knows about one changed file.

    ``head_content`` is the file after the PR (``None`` if unknown).
    ``context_windows`` are inclusive ``(start, end)`` head-line ranges
    shown to the model with line numbers. ``base_content`` is the indexed
    pre-PR copy, used only as unnumbered fallback context.
    """

    file_path: str
    diff_hunk: DiffHunk
    indexed: bool
    language: str | None = None
    head_content: str | None = None
    head_line_count: int | None = None
    context_windows: list[tuple[int, int]] = field(default_factory=list)
    base_content: str | None = None
    symbols_near_changes: list[SymbolInDiff] = field(default_factory=list)
    imported_by: list[str] = field(default_factory=list)

    def visible_lines(self) -> set[int]:
        """Head line numbers the model could cite from what it was shown."""
        lines: set[int] = set()
        for r in self.diff_hunk.changed_ranges:
            lines.update(range(r.start, r.end + 1))
        for start, end in self.context_windows:
            lines.update(range(start, end + 1))
        return lines


@dataclass
class DiffContextResult:
    """Full context for every file changed in a PR."""

    repo_name: str
    files: list[FileContext]
    indexed_count: int
    total_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def count_lines(content: str) -> int:
    """Line count the way git and GitHub count it (``\\n`` only)."""
    if not content:
        return 0
    return content.count("\n") + (0 if content.endswith("\n") else 1)


def language_for(path: str) -> str | None:
    return LANGUAGE_BY_EXT.get(PurePosixPath(path).suffix.lower())


def _extract_symbols_with_lines(structure: dict) -> list[SymbolInDiff]:
    symbols: list[SymbolInDiff] = []
    for cls in structure.get("classes", []):
        symbols.append(
            SymbolInDiff(
                name=cls["name"],
                kind=cls.get("kind", "class"),
                start_line=cls["start_line"],
                end_line=cls["end_line"],
            )
        )
    for fn in structure.get("functions", []):
        symbols.append(
            SymbolInDiff(
                name=fn["name"],
                kind=fn.get("kind", "function"),
                start_line=fn["start_line"],
                end_line=fn["end_line"],
                parent_class=fn.get("parent_class"),
            )
        )
    symbols.sort(key=lambda s: s.start_line)
    return symbols


def _overlaps(symbol: SymbolInDiff, ranges: list[ChangedLineRange]) -> bool:
    return any(symbol.start_line <= r.end and symbol.end_line >= r.start for r in ranges)


def _symbols_near_changes(
    structure: dict, changed_ranges: list[ChangedLineRange]
) -> list[SymbolInDiff]:
    if not changed_ranges:
        return []
    return [s for s in _extract_symbols_with_lines(structure) if _overlaps(s, changed_ranges)]


def context_windows(
    line_count: int, changed_ranges: list[ChangedLineRange]
) -> list[tuple[int, int]]:
    """Head-line ranges to show: the whole file, or merged windows."""
    if line_count <= 0:
        return []
    if line_count <= _WHOLE_FILE_MAX_LINES or not changed_ranges:
        return [(1, min(line_count, _WHOLE_FILE_MAX_LINES))]
    windows = sorted(
        (max(1, r.start - _WINDOW_RADIUS), min(line_count, r.end + _WINDOW_RADIUS))
        for r in changed_ranges
    )
    merged: list[tuple[int, int]] = [windows[0]]
    for start, end in windows[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _head_symbols(path: str, content: str, language: str | None, ranges) -> list[SymbolInDiff]:
    if language is None:
        return []
    structure = parse_file(Path(path), content, language).to_jsonb()
    return _symbols_near_changes(structure, ranges)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def diff_context_view(
    session: AsyncSession,
    repo_name: str,
    diff_hunks: list[DiffHunk],
    *,
    head_contents: dict[str, str] | None = None,
) -> DiffContextResult:
    """Build reviewer context for each file changed in a PR."""
    repo = await resolve_repo(session, repo_name)
    head_contents = head_contents or {}

    if not diff_hunks:
        return DiffContextResult(repo_name=repo_name, files=[], indexed_count=0, total_count=0)

    changed_paths = [h.file_path for h in diff_hunks]
    stmt = (
        select(CodeIndex)
        .where(CodeIndex.repo_id == repo.id)
        .where(CodeIndex.file_path.in_(changed_paths))
    )
    rows = {row.file_path: row for row in (await session.execute(stmt)).scalars().all()}

    edge_stmt = (
        select(ImportEdge.dst_file, ImportEdge.src_file)
        .where(ImportEdge.repo_id == repo.id)
        .where(ImportEdge.dst_file.in_(changed_paths))
    )
    imported_by_map: dict[str, list[str]] = {}
    for dst, src in (await session.execute(edge_stmt)).all():
        imported_by_map.setdefault(dst, []).append(src)

    files: list[FileContext] = []
    indexed_count = 0
    for hunk in diff_hunks:
        row = rows.get(hunk.file_path)
        if row is not None:
            indexed_count += 1
        language = language_for(hunk.file_path) or (row.language if row else None)
        ctx = FileContext(
            file_path=hunk.file_path,
            diff_hunk=hunk,
            indexed=row is not None,
            language=language,
            imported_by=imported_by_map.get(hunk.file_path, []),
        )

        head = head_contents.get(hunk.file_path)
        if head is not None:
            ctx.head_content = head
            ctx.head_line_count = count_lines(head)
            ctx.context_windows = context_windows(ctx.head_line_count, hunk.changed_ranges)
            ctx.symbols_near_changes = _head_symbols(
                hunk.file_path, head, language, hunk.changed_ranges
            )
        elif row is not None and hunk.status != "added":
            # Base version only: unnumbered context, no head-side symbols.
            ctx.base_content = row.content

        files.append(ctx)

    return DiffContextResult(
        repo_name=repo_name,
        files=files,
        indexed_count=indexed_count,
        total_count=len(diff_hunks),
    )
