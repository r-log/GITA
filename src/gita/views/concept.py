"""``concept_view`` — natural-language search over indexed code.

Given a query like "authentication" or "database connection handling",
returns the most relevant files ranked by a combination of Postgres
full-text search and (optionally) pgvector semantic similarity, with
highlighted snippets and the symbols defined in each matching file.

**Two modes:**

1. **FTS-only** (default) — Postgres ``tsvector`` / ``tsquery`` with the
   ``simple`` text config (no English stemming — code identifiers like
   ``get_user_by_name`` match ``user`` literally). Used when no
   ``embedding_client`` is passed, when the repo has no file embeddings
   populated, or when the query can't be embedded.

2. **Hybrid FTS + semantic** — when an embedding client is supplied
   AND at least one file in the repo has a populated ``embedding``,
   the query is embedded and the two result sets are merged. A file's
   combined score is a weighted sum of its (normalized) FTS rank and
   its cosine similarity to the query embedding, plus the existing
   symbol-match boost.

Semantic-only hits use ``ts_headline`` for display; when the query
terms don't appear in the file, ``ts_headline`` falls back to the
first fragment of the content without highlights — acceptable for CLI
output and explicit that the match is vibes-based.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex
from gita.indexer.embeddings import EmbeddingClient
from gita.views._common import SymbolBrief, build_symbol_summary, resolve_repo

logger = logging.getLogger(__name__)

_MAX_RESULTS = 10
_HEADLINE_OPTS = "StartSel=**, StopSel=**, MaxFragments=3, MaxWords=30, MinWords=10"

# Symbol-name boost: files where a function/class name matches the query
# get an additive rank boost. This promotes files that DEFINE a concept
# over files that merely mention it in a comment.
_SYMBOL_BOOST = 0.3   # per matching symbol
_MAX_SYMBOL_BOOST = 1.0  # cap so one file with 10 matching symbols doesn't dominate

# Hybrid scoring weights. FTS tends to give confident high scores for
# keyword matches but misses paraphrases; vector similarity catches
# paraphrases but can drift to loosely related files. Equal weighting
# is a reasonable starting point — tune once we have usage data.
_W_FTS = 0.5
_W_VECTOR = 0.5

# Cosine-distance ceiling for a semantic hit. pgvector's ``<=>`` returns
# distance in [0, 2]; lower is more similar. Unit-norm embeddings from
# OpenAI + actually-relevant code typically land under 0.5. Keeping this
# strict prevents nonsense queries (e.g. "xyznonexistent") from pulling
# in random files just because they happened to be closest in vector
# space.
_SEMANTIC_DISTANCE_MAX = 0.5

# How many candidates to pull from each side before merging + truncating
# to ``limit``. 3x gives some overlap headroom when the two rankings
# diverge without inflating the result set.
_CANDIDATE_MULTIPLIER = 3


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
    mode: str = "fts"  # "fts" | "hybrid"


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


async def _repo_has_embeddings(
    session: AsyncSession, repo_id: Any
) -> bool:
    """Fast existence check — is semantic search usable for this repo?"""
    stmt = (
        select(func.count())
        .select_from(CodeIndex)
        .where(CodeIndex.repo_id == repo_id)
        .where(CodeIndex.embedding.is_not(None))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one() > 0


async def _fts_candidates(
    session: AsyncSession,
    repo_id: Any,
    query: str,
    candidate_limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Run the FTS half of the search.

    Returns ``(rows, total_match_count)`` where ``rows`` is a list of
    dicts with ``file_path``, ``language``, ``line_count``, ``structure``,
    ``fts_rank``, and ``headline`` keys.
    """
    tsquery = func.plainto_tsquery(literal_column("'simple'"), query)
    tsvec = func.to_tsvector(
        literal_column("'simple'"),
        func.coalesce(CodeIndex.content, ""),
    )

    count_stmt = (
        select(func.count())
        .select_from(CodeIndex)
        .where(CodeIndex.repo_id == repo_id)
        .where(tsvec.op("@@")(tsquery))
    )
    total = (await session.execute(count_stmt)).scalar_one()

    if total == 0:
        return [], 0

    rank = func.ts_rank_cd(tsvec, tsquery)
    headline = func.ts_headline(
        literal_column("'simple'"),
        func.coalesce(CodeIndex.content, ""),
        tsquery,
        literal_column(f"'{_HEADLINE_OPTS}'"),
    )

    stmt = (
        select(
            CodeIndex.file_path,
            CodeIndex.language,
            CodeIndex.line_count,
            CodeIndex.structure,
            rank.label("fts_rank"),
            headline.label("headline"),
        )
        .where(CodeIndex.repo_id == repo_id)
        .where(tsvec.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(candidate_limit)
    )
    rows = [dict(r._mapping) for r in (await session.execute(stmt)).all()]
    return rows, total


async def _semantic_candidates(
    session: AsyncSession,
    repo_id: Any,
    query: str,
    query_vec: list[float],
    candidate_limit: int,
) -> list[dict[str, Any]]:
    """Run the vector-similarity half of the search.

    Returns rows with ``file_path``, ``language``, ``line_count``,
    ``structure``, ``distance``, and ``headline`` keys. Rows whose
    cosine distance exceeds ``_SEMANTIC_DISTANCE_MAX`` are dropped so
    junk queries don't surface unrelated files.
    """
    tsquery = func.plainto_tsquery(literal_column("'simple'"), query)
    headline = func.ts_headline(
        literal_column("'simple'"),
        func.coalesce(CodeIndex.content, ""),
        tsquery,
        literal_column(f"'{_HEADLINE_OPTS}'"),
    )
    distance = CodeIndex.embedding.cosine_distance(query_vec).label("distance")

    stmt = (
        select(
            CodeIndex.file_path,
            CodeIndex.language,
            CodeIndex.line_count,
            CodeIndex.structure,
            distance,
            headline.label("headline"),
        )
        .where(CodeIndex.repo_id == repo_id)
        .where(CodeIndex.embedding.is_not(None))
        .order_by(distance)
        .limit(candidate_limit)
    )
    rows = [dict(r._mapping) for r in (await session.execute(stmt)).all()]
    return [r for r in rows if r["distance"] is not None and r["distance"] <= _SEMANTIC_DISTANCE_MAX]


def _normalize_fts_ranks(rows: list[dict[str, Any]]) -> None:
    """Rescale ``fts_rank`` in-place to the [0, 1] range.

    ``ts_rank_cd`` has no natural upper bound — it depends on document
    length and term density. We divide by the max in the candidate set
    so the FTS and vector components contribute comparably to the
    hybrid score.
    """
    if not rows:
        return
    max_rank = max(r["fts_rank"] for r in rows)
    if max_rank <= 0:
        return
    for r in rows:
        r["fts_rank_normalized"] = r["fts_rank"] / max_rank


async def concept_view(
    session: AsyncSession,
    repo_name: str,
    query: str,
    *,
    limit: int = _MAX_RESULTS,
    embedding_client: EmbeddingClient | None = None,
) -> ConceptResult:
    """Search indexed code by natural-language query.

    FTS-only when ``embedding_client`` is ``None`` or when the repo has
    no embeddings populated; hybrid FTS + vector-similarity otherwise.
    """
    repo = await resolve_repo(session, repo_name)

    # Normalize query terms for Python-side symbol matching.
    query_terms = [t.lower() for t in query.split() if len(t) >= 2]

    candidate_limit = max(limit * _CANDIDATE_MULTIPLIER, limit)

    # --- FTS side (always runs) ---
    fts_rows, fts_total = await _fts_candidates(
        session, repo.id, query, candidate_limit
    )
    _normalize_fts_ranks(fts_rows)

    # --- Semantic side (optional) ---
    semantic_rows: list[dict[str, Any]] = []
    use_hybrid = False
    if embedding_client is not None:
        try:
            if await _repo_has_embeddings(session, repo.id):
                query_vec = (await embedding_client.embed([query]))[0]
                semantic_rows = await _semantic_candidates(
                    session, repo.id, query, query_vec, candidate_limit
                )
                use_hybrid = True
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "semantic_search_failed query=%r error=%s falling_back_to_fts",
                query,
                exc,
            )
            semantic_rows = []
            use_hybrid = False

    # --- Merge + score ---
    merged: dict[str, dict[str, Any]] = {}

    for row in fts_rows:
        merged[row["file_path"]] = {
            **row,
            "fts_rank_normalized": row.get("fts_rank_normalized", 0.0),
            "vector_score": 0.0,
            "vector_hit": False,
        }

    for row in semantic_rows:
        # Cosine distance in [0, 2]; convert to similarity in [0, 1]:
        # identical → 1.0, orthogonal → 0.5, opposite → 0.0.
        similarity = max(0.0, 1.0 - row["distance"] / 2.0)
        existing = merged.get(row["file_path"])
        if existing is not None:
            existing["vector_score"] = similarity
            existing["vector_hit"] = True
        else:
            merged[row["file_path"]] = {
                "file_path": row["file_path"],
                "language": row["language"],
                "line_count": row["line_count"],
                "structure": row["structure"],
                "fts_rank": 0.0,
                "fts_rank_normalized": 0.0,
                "vector_score": similarity,
                "vector_hit": True,
                "headline": row["headline"],
            }

    if not merged:
        return ConceptResult(
            query=query,
            repo_name=repo_name,
            matches=[],
            total_matches=0,
            mode="hybrid" if use_hybrid else "fts",
        )

    matches: list[ConceptMatch] = []
    for row in merged.values():
        all_symbols = build_symbol_summary(row.get("structure") or {})
        matching_syms = _symbols_matching_query(all_symbols, query_terms)

        symbol_boost = min(
            len(matching_syms) * _SYMBOL_BOOST, _MAX_SYMBOL_BOOST
        )
        if use_hybrid:
            combined = (
                _W_FTS * row["fts_rank_normalized"]
                + _W_VECTOR * row["vector_score"]
            )
        else:
            # FTS-only: preserve legacy behavior — use raw ts_rank_cd.
            combined = row["fts_rank"]
        final_rank = combined + symbol_boost

        matches.append(
            ConceptMatch(
                file_path=row["file_path"],
                language=row["language"],
                rank=round(final_rank, 4),
                headline=row["headline"] or "",
                line_count=row["line_count"],
                symbols=all_symbols,
                matching_symbols=matching_syms,
            )
        )

    matches.sort(key=lambda m: m.rank, reverse=True)
    matches = matches[:limit]

    return ConceptResult(
        query=query,
        repo_name=repo_name,
        matches=matches,
        total_matches=len(merged),
        mode="hybrid" if use_hybrid else "fts",
    )
