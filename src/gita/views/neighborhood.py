"""``neighborhood_view`` — metadata navigation around a file.

Given a file path, return:
- The file itself (path, language, line count, symbol summary)
- Files this file imports (resolved only)
- Files that import this file
- Sibling files in the same directory (cap 10)
- Raw import strings that could not be resolved

**No file content.** If an agent wants code, it calls ``symbol_view`` after
using the neighborhood to orient itself. This keeps payloads bounded and
follows the "views are navigation, symbol_view returns code" pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex, ImportEdge
from gita.views._common import resolve_repo

MAX_IMPORTS = 20
MAX_IMPORTED_BY = 20
MAX_SIBLINGS = 10


@dataclass
class SymbolBrief:
    name: str
    kind: str
    line: int
    parent_class: str | None = None


@dataclass
class FileInfo:
    file_path: str
    language: str
    line_count: int
    symbol_summary: list[SymbolBrief] = field(default_factory=list)


@dataclass
class NeighborhoodResult:
    file: FileInfo
    imports: list[FileInfo] = field(default_factory=list)
    imported_by: list[FileInfo] = field(default_factory=list)
    siblings: list[FileInfo] = field(default_factory=list)
    unresolved_imports: list[str] = field(default_factory=list)


class FileNotFoundError(LookupError):
    """Raised when neighborhood_view is asked about a file that isn't indexed."""


def _build_symbol_summary(structure: dict) -> list[SymbolBrief]:
    """Flatten the JSONB structure into a simple list of briefs."""
    briefs: list[SymbolBrief] = []
    for cls in structure.get("classes", []):
        briefs.append(
            SymbolBrief(
                name=cls["name"],
                kind=cls["kind"],
                line=cls["start_line"],
            )
        )
    for fn in structure.get("functions", []):
        briefs.append(
            SymbolBrief(
                name=fn["name"],
                kind=fn["kind"],
                line=fn["start_line"],
                parent_class=fn.get("parent_class"),
            )
        )
    briefs.sort(key=lambda b: b.line)
    return briefs


def _row_to_file_info(row: CodeIndex) -> FileInfo:
    return FileInfo(
        file_path=row.file_path,
        language=row.language,
        line_count=row.line_count,
        symbol_summary=_build_symbol_summary(row.structure or {}),
    )


async def neighborhood_view(
    session: AsyncSession, repo_name: str, file_path: str
) -> NeighborhoodResult:
    """Return navigation metadata around ``file_path``."""
    repo = await resolve_repo(session, repo_name)

    # Normalize forward slashes so callers can pass Windows-style paths
    file_path = file_path.replace("\\", "/")

    stmt = select(CodeIndex).where(
        CodeIndex.repo_id == repo.id, CodeIndex.file_path == file_path
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise FileNotFoundError(
            f"file not indexed in {repo_name!r}: {file_path!r}"
        )

    file_info = _row_to_file_info(row)

    # Imports from this file
    imports_stmt = (
        select(ImportEdge)
        .where(ImportEdge.repo_id == repo.id, ImportEdge.src_file == file_path)
    )
    out_edges = (await session.execute(imports_stmt)).scalars().all()

    resolved_out_paths = [e.dst_file for e in out_edges if e.dst_file]
    unresolved = [e.raw_import for e in out_edges if e.dst_file is None]

    imports: list[FileInfo] = []
    if resolved_out_paths:
        stmt = (
            select(CodeIndex)
            .where(CodeIndex.repo_id == repo.id)
            .where(CodeIndex.file_path.in_(resolved_out_paths[:MAX_IMPORTS]))
        )
        imports = [
            _row_to_file_info(r)
            for r in (await session.execute(stmt)).scalars().all()
        ]

    # Imports into this file (reverse dependencies)
    importers_stmt = (
        select(ImportEdge)
        .where(ImportEdge.repo_id == repo.id, ImportEdge.dst_file == file_path)
    )
    in_edges = (await session.execute(importers_stmt)).scalars().all()
    importer_paths = [e.src_file for e in in_edges][:MAX_IMPORTED_BY]

    imported_by: list[FileInfo] = []
    if importer_paths:
        stmt = (
            select(CodeIndex)
            .where(CodeIndex.repo_id == repo.id)
            .where(CodeIndex.file_path.in_(importer_paths))
        )
        imported_by = [
            _row_to_file_info(r)
            for r in (await session.execute(stmt)).scalars().all()
        ]

    # Sibling files in the same directory
    parent = PurePosixPath(file_path).parent
    parent_str = str(parent) if str(parent) != "." else ""
    sibling_stmt = (
        select(CodeIndex)
        .where(CodeIndex.repo_id == repo.id)
        .where(CodeIndex.file_path != file_path)
    )
    all_rows = (await session.execute(sibling_stmt)).scalars().all()
    siblings: list[FileInfo] = []
    for r in all_rows:
        r_parent = str(PurePosixPath(r.file_path).parent)
        if r_parent == "." and parent_str == "":
            siblings.append(_row_to_file_info(r))
        elif r_parent == parent_str:
            siblings.append(_row_to_file_info(r))
        if len(siblings) >= MAX_SIBLINGS:
            break

    return NeighborhoodResult(
        file=file_info,
        imports=imports,
        imported_by=imported_by,
        siblings=siblings,
        unresolved_imports=unresolved,
    )
