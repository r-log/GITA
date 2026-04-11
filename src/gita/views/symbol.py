"""``symbol_view`` — find a symbol by name, return its code.

This is the only view that returns actual source code. Everything else in the
view layer is navigation metadata.

Query syntax:
    ``foo``              — match any symbol named ``foo``
    ``ClassName.method`` — match method ``method`` whose parent class is
                           ``ClassName``

Multiple matches are returned (e.g. two files define ``format_name``). We cap
at 10 matches to avoid runaway; ``total_matches`` reports the true count.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex
from gita.views._common import resolve_repo

MAX_MATCHES = 10


@dataclass
class SymbolMatch:
    file_path: str
    kind: str
    name: str
    parent_class: str | None
    start_line: int
    end_line: int
    code: str  # lines prepended with 1-indexed line numbers


@dataclass
class SymbolResult:
    query: str
    matches: list[SymbolMatch] = field(default_factory=list)
    total_matches: int = 0

    @property
    def truncated(self) -> bool:
        return self.total_matches > len(self.matches)


def _parse_query(query: str) -> tuple[str | None, str]:
    """Split a query on the first dot: ``ClassName.method`` → (ClassName, method)."""
    if "." in query:
        parent, name = query.split(".", 1)
        return parent.strip() or None, name.strip()
    return None, query.strip()


def _slice_code(content: str, start_line: int, end_line: int) -> str:
    """Return lines ``start_line..end_line`` (1-indexed, inclusive) with line
    numbers prepended in a fixed-width column."""
    lines = content.splitlines()
    start = max(0, start_line - 1)
    end = min(len(lines), end_line)
    width = len(str(end))
    out = []
    for i, line in enumerate(lines[start:end], start=start + 1):
        out.append(f"{i:>{width}}: {line}")
    return "\n".join(out)


def _match_symbol(
    structure: dict,
    file_path: str,
    content: str,
    parent_filter: str | None,
    name: str,
) -> list[SymbolMatch]:
    """Scan one file's structure JSONB for symbols matching name + parent."""
    matches: list[SymbolMatch] = []

    # Check classes
    for cls in structure.get("classes", []):
        if cls["name"] != name:
            continue
        if parent_filter is not None:
            # ClassName.foo can't match a class; skip.
            continue
        matches.append(
            SymbolMatch(
                file_path=file_path,
                kind=cls["kind"],
                name=cls["name"],
                parent_class=None,
                start_line=cls["start_line"],
                end_line=cls["end_line"],
                code=_slice_code(content, cls["start_line"], cls["end_line"]),
            )
        )

    # Check functions/methods
    for fn in structure.get("functions", []):
        if fn["name"] != name:
            continue
        if parent_filter is not None and fn.get("parent_class") != parent_filter:
            continue
        matches.append(
            SymbolMatch(
                file_path=file_path,
                kind=fn["kind"],
                name=fn["name"],
                parent_class=fn.get("parent_class"),
                start_line=fn["start_line"],
                end_line=fn["end_line"],
                code=_slice_code(content, fn["start_line"], fn["end_line"]),
            )
        )

    return matches


async def symbol_view(
    session: AsyncSession, repo_name: str, query: str
) -> SymbolResult:
    """Look up a symbol by name (optionally ``ClassName.method``)."""
    repo = await resolve_repo(session, repo_name)
    parent_filter, name = _parse_query(query)

    if not name:
        return SymbolResult(query=query)

    stmt = select(CodeIndex).where(CodeIndex.repo_id == repo.id)
    rows = (await session.execute(stmt)).scalars().all()

    all_matches: list[SymbolMatch] = []
    for row in rows:
        if row.content is None:
            continue
        all_matches.extend(
            _match_symbol(
                row.structure or {},
                row.file_path,
                row.content,
                parent_filter,
                name,
            )
        )

    return SymbolResult(
        query=query,
        matches=all_matches[:MAX_MATCHES],
        total_matches=len(all_matches),
    )
