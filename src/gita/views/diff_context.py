"""Diff context view — maps PR diff hunks to indexed code context.

Given a list of ``DiffHunk`` objects (from ``diff_parser.parse_pr_files``),
this view looks up each changed file in ``code_index`` and returns:

- Whether the file is indexed at all (graceful fallback if not)
- Which symbols (functions, classes) overlap the changed line ranges
- Which other files import this one (reverse-dep impact signal)
- The full file content (for surrounding-code context in the prompt)

The view is the bridge between "what the PR changed" (the diff) and
"what the codebase knows about those files" (the index). The PR reviewer
agent feeds both into the LLM so the review has structural context, not
just raw patch text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.pr_reviewer.diff_parser import ChangedLineRange, DiffHunk
from gita.db.models import CodeIndex, ImportEdge
from gita.views._common import SymbolBrief, resolve_repo

logger = logging.getLogger(__name__)


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
    """Context for one file changed in the PR.

    ``indexed=False`` means the file isn't in ``code_index`` — the agent
    still sees the raw diff patch but has no symbol or neighborhood context.
    """

    file_path: str
    diff_hunk: DiffHunk
    indexed: bool
    language: str | None = None
    line_count: int | None = None
    content: str | None = None
    symbols_near_changes: list[SymbolInDiff] = field(default_factory=list)
    all_symbols: list[SymbolBrief] = field(default_factory=list)
    imported_by: list[str] = field(default_factory=list)


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
def _extract_symbols_with_lines(structure: dict) -> list[SymbolInDiff]:
    """Extract symbols with start/end line ranges from code_index.structure."""
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


def _overlaps(
    symbol: SymbolInDiff, ranges: list[ChangedLineRange]
) -> bool:
    """True if the symbol's line span overlaps any changed range.

    Interval overlap: ``sym.start <= range.end AND sym.end >= range.start``
    """
    for r in ranges:
        if symbol.start_line <= r.end and symbol.end_line >= r.start:
            return True
    return False


def _symbols_near_changes(
    structure: dict, changed_ranges: list[ChangedLineRange]
) -> list[SymbolInDiff]:
    """Return symbols from ``structure`` that overlap the changed ranges."""
    if not changed_ranges:
        return []
    all_symbols = _extract_symbols_with_lines(structure)
    return [s for s in all_symbols if _overlaps(s, changed_ranges)]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def diff_context_view(
    session: AsyncSession,
    repo_name: str,
    diff_hunks: list[DiffHunk],
) -> DiffContextResult:
    """Build context for each file changed in a PR.

    One ``code_index`` batch-fetch for all changed file paths, plus one
    ``import_edges`` batch-fetch for reverse dependencies. Files not in
    the index get ``indexed=False`` with no symbol/content context.
    """
    repo = await resolve_repo(session, repo_name)

    if not diff_hunks:
        return DiffContextResult(
            repo_name=repo_name,
            files=[],
            indexed_count=0,
            total_count=0,
        )

    # Batch-fetch all changed files from the index.
    changed_paths = [h.file_path for h in diff_hunks]
    stmt = (
        select(CodeIndex)
        .where(CodeIndex.repo_id == repo.id)
        .where(CodeIndex.file_path.in_(changed_paths))
    )
    rows = {
        row.file_path: row
        for row in (await session.execute(stmt)).scalars().all()
    }

    # Batch-fetch reverse dependencies (who imports these files?).
    edge_stmt = (
        select(ImportEdge.dst_file, ImportEdge.src_file)
        .where(ImportEdge.repo_id == repo.id)
        .where(ImportEdge.dst_file.in_(changed_paths))
    )
    imported_by_map: dict[str, list[str]] = {}
    for dst, src in (await session.execute(edge_stmt)).all():
        imported_by_map.setdefault(dst, []).append(src)

    # Build context per file.
    files: list[FileContext] = []
    indexed_count = 0

    for hunk in diff_hunks:
        row = rows.get(hunk.file_path)
        if row is None:
            files.append(
                FileContext(
                    file_path=hunk.file_path,
                    diff_hunk=hunk,
                    indexed=False,
                )
            )
            continue

        indexed_count += 1
        structure = row.structure or {}

        from gita.views._common import build_symbol_summary

        files.append(
            FileContext(
                file_path=hunk.file_path,
                diff_hunk=hunk,
                indexed=True,
                language=row.language,
                line_count=row.line_count,
                content=row.content,
                symbols_near_changes=_symbols_near_changes(
                    structure, hunk.changed_ranges
                ),
                all_symbols=build_symbol_summary(structure),
                imported_by=imported_by_map.get(hunk.file_path, []),
            )
        )

    return DiffContextResult(
        repo_name=repo_name,
        files=files,
        indexed_count=indexed_count,
        total_count=len(diff_hunks),
    )
