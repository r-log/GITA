"""``load_bearing_view`` — rank files by how many other files import them.

This is the first-step view for the onboarding agent. Given a repo, return
the top N files ranked by in-degree in the import graph (files that lots of
other files depend on are load-bearing by definition).

Strategy: a single LEFT JOIN against a GROUP BY subquery of ``import_edges``.
Files with no incoming edges still appear in the result (padded with
``in_degree=0``) so the agent always gets N files to consider. Tie-break by
``file_path`` alphabetically for determinism.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex, ImportEdge
from gita.views._common import (
    SymbolBrief,
    build_symbol_summary,
    resolve_repo,
)

DEFAULT_LIMIT = 10
MAX_LIMIT = 100


@dataclass
class RankedFile:
    file_path: str
    language: str
    line_count: int
    in_degree: int
    symbol_summary: list[SymbolBrief] = field(default_factory=list)


@dataclass
class LoadBearingResult:
    repo_name: str
    limit: int
    files: list[RankedFile] = field(default_factory=list)
    total_files: int = 0  # total files in the repo (for context)


async def load_bearing_view(
    session: AsyncSession,
    repo_name: str,
    limit: int = DEFAULT_LIMIT,
) -> LoadBearingResult:
    """Return files ranked by in-degree in the import graph."""
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")
    limit = min(limit, MAX_LIMIT)

    repo = await resolve_repo(session, repo_name)

    # Subquery: per-dst_file count of import edges in this repo
    in_degree_subq = (
        select(
            ImportEdge.dst_file.label("dst"),
            func.count(ImportEdge.id).label("deg"),
        )
        .where(ImportEdge.repo_id == repo.id)
        .where(ImportEdge.dst_file.is_not(None))
        .group_by(ImportEdge.dst_file)
        .subquery()
    )

    in_degree_col = func.coalesce(in_degree_subq.c.deg, 0).label("in_degree")

    stmt = (
        select(CodeIndex, in_degree_col)
        .outerjoin(
            in_degree_subq, in_degree_subq.c.dst == CodeIndex.file_path
        )
        .where(CodeIndex.repo_id == repo.id)
        .order_by(in_degree_col.desc(), CodeIndex.file_path.asc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    ranked: list[RankedFile] = []
    for row in rows:
        code_row = row[0]
        in_degree = int(row[1])
        ranked.append(
            RankedFile(
                file_path=code_row.file_path,
                language=code_row.language,
                line_count=code_row.line_count,
                in_degree=in_degree,
                symbol_summary=build_symbol_summary(code_row.structure or {}),
            )
        )

    total_stmt = (
        select(func.count(CodeIndex.id)).where(CodeIndex.repo_id == repo.id)
    )
    total = int((await session.execute(total_stmt)).scalar_one())

    return LoadBearingResult(
        repo_name=repo_name,
        limit=limit,
        files=ranked,
        total_files=total,
    )
