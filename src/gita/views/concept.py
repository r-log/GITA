"""``concept_view`` — natural-language search over indexed code.

Given a query like "authentication" or "database connection handling",
returns the most relevant files ranked by Postgres full-text search, with
highlighted snippets and the symbols defined in each matching file.

**v1 is keyword-based** using Postgres ``tsvector`` / ``tsquery`` with the
``simple`` text config (no English stemming — code identifiers like
``get_user_by_name`` match ``user`` literally). The interface is designed
so embeddings can replace the backend in a future week without changing
the API contract.

**How ranking works:**
1. ``plainto_tsquery('simple', query)`` converts the user's input into
   an AND query (all words must match).
2. ``ts_rank_cd`` scores each file by how densely the terms appear.
3. Symbol-name matching runs Python-side: symbols whose name contains
   any query term get boosted to the top of the file's symbol list.
4. ``ts_headline`` generates a snippet with matching terms marked by
   ``**...**`` for CLI display.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex
from gita.views._common import SymbolBrief, build_symbol_summary, resolve_repo

logger = logging.getLogger(__name__)

_MAX_RESULTS = 10
_HEADLINE_OPTS = "StartSel=**, StopSel=**, MaxFragments=3, MaxWords=30, MinWords=10"

# Symbol-name boost: files where a function/class name matches the query
# get an additive rank boost. This promotes files that DEFINE a concept
# over files that merely mention it in a comment.
_SYMBOL_BOOST = 0.3   # per matching symbol
_MAX_SYMBOL_BOOST = 1.0  # cap so one file with 10 matching symbols doesn't dominate


@dataclass
class ConceptMatch:
    """One file matching a concept query."""

    file_path: str
    language: str
    rank: float
    headline: str  # snippet with **matched terms**
    line_count: int
    symbols: list[SymbolBrief] = field(default_factory=list)
    matching_symbols: list[SymbolBrief] = field(default_factory=list)


@dataclass
class ConceptResult:
    """Result of a concept_view query."""

    query: str
    repo_name: str
    matches: list[ConceptMatch]
    total_matches: int


def _symbols_matching_query(
    symbols: list[SymbolBrief], query_terms: list[str]
) -> list[SymbolBrief]:
    """Return symbols whose name contains any of the query terms."""
    if not query_terms:
        return []
    matching: list[SymbolBrief] = []
    for sym in symbols:
        name_lower = sym.name.lower()
        if any(term in name_lower for term in query_terms):
            matching.append(sym)
    return matching


async def concept_view(
    session: AsyncSession,
    repo_name: str,
    query: str,
    *,
    limit: int = _MAX_RESULTS,
) -> ConceptResult:
    """Search indexed code by natural-language query.

    Uses Postgres full-text search (``plainto_tsquery``) against the
    GIN-indexed ``code_index.content`` column. Returns ranked results
    with highlighted snippets and per-file symbol lists.
    """
    repo = await resolve_repo(session, repo_name)

    # Normalize query terms for Python-side symbol matching.
    query_terms = [t.lower() for t in query.split() if len(t) >= 2]

    # Build the tsquery from the user's input.
    tsquery = func.plainto_tsquery(literal_column("'simple'"), query)
    tsvec = func.to_tsvector(
        literal_column("'simple'"),
        func.coalesce(CodeIndex.content, ""),
    )

    # Count total matches first.
    count_stmt = (
        select(func.count())
        .select_from(CodeIndex)
        .where(CodeIndex.repo_id == repo.id)
        .where(tsvec.op("@@")(tsquery))
    )
    total_matches = (await session.execute(count_stmt)).scalar_one()

    if total_matches == 0:
        return ConceptResult(
            query=query,
            repo_name=repo_name,
            matches=[],
            total_matches=0,
        )

    # Fetch ranked results with headlines.
    rank = func.ts_rank_cd(tsvec, tsquery)
    headline = func.ts_headline(
        literal_column("'simple'"),
        func.coalesce(CodeIndex.content, ""),
        tsquery,
        literal_column(f"'{_HEADLINE_OPTS}'"),
    )

    results_stmt = (
        select(
            CodeIndex.file_path,
            CodeIndex.language,
            CodeIndex.line_count,
            CodeIndex.structure,
            rank.label("rank"),
            headline.label("headline"),
        )
        .where(CodeIndex.repo_id == repo.id)
        .where(tsvec.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(limit)
    )
    rows = (await session.execute(results_stmt)).all()

    matches: list[ConceptMatch] = []
    for row in rows:
        all_symbols = build_symbol_summary(row.structure or {})
        matching_syms = _symbols_matching_query(all_symbols, query_terms)

        # Boost rank when symbols match the query. A file that DEFINES
        # a function named after the query term is more relevant than one
        # that merely mentions it in a comment. The boost is additive so
        # it can promote a lower-ranked FTS hit above a higher one.
        fts_rank = float(row.rank)
        symbol_boost = min(len(matching_syms) * _SYMBOL_BOOST, _MAX_SYMBOL_BOOST)
        boosted_rank = fts_rank + symbol_boost

        matches.append(
            ConceptMatch(
                file_path=row.file_path,
                language=row.language,
                rank=round(boosted_rank, 4),
                headline=row.headline,
                line_count=row.line_count,
                symbols=all_symbols,
                matching_symbols=matching_syms,
            )
        )

    # Re-sort after boosting — symbol matches may have promoted files.
    matches.sort(key=lambda m: m.rank, reverse=True)

    return ConceptResult(
        query=query,
        repo_name=repo_name,
        matches=matches,
        total_matches=total_matches,
    )
