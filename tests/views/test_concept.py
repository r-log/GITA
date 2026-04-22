"""Tests for concept_view — full-text search over indexed code.

Uses the ``indexed_synth_py`` fixture which indexes the synthetic_py
project into the test DB. Queries are run against the known file
contents to verify ranking, snippets, and symbol matching.

Synthetic_py files:
- src/myapp/utils.py:   format_name, validate_email
- src/myapp/models.py:  User class, display_name, has_valid_email
- src/myapp/core.py:    create_user, main
- src/myapp/__init__.py: empty
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from gita.views.concept import (
    ConceptResult,
    _symbols_matching_query,
    concept_view,
)
from gita.views._common import SymbolBrief


# ---------------------------------------------------------------------------
# Pure helper: _symbols_matching_query
# ---------------------------------------------------------------------------
class TestSymbolsMatchingQuery:
    def test_matches_by_name_substring(self):
        symbols = [
            SymbolBrief(name="format_name", kind="function", line=1),
            SymbolBrief(name="validate_email", kind="function", line=5),
        ]
        result = _symbols_matching_query(symbols, ["format"])
        assert len(result) == 1
        assert result[0].name == "format_name"

    def test_matches_multiple_terms(self):
        symbols = [
            SymbolBrief(name="format_name", kind="function", line=1),
            SymbolBrief(name="validate_email", kind="function", line=5),
        ]
        result = _symbols_matching_query(symbols, ["format", "email"])
        assert len(result) == 2

    def test_case_insensitive(self):
        symbols = [
            SymbolBrief(name="User", kind="class", line=6),
        ]
        result = _symbols_matching_query(symbols, ["user"])
        assert len(result) == 1

    def test_no_match_returns_empty(self):
        symbols = [
            SymbolBrief(name="format_name", kind="function", line=1),
        ]
        result = _symbols_matching_query(symbols, ["zzz"])
        assert result == []

    def test_empty_terms(self):
        symbols = [
            SymbolBrief(name="format_name", kind="function", line=1),
        ]
        assert _symbols_matching_query(symbols, []) == []


# ---------------------------------------------------------------------------
# DB-integrated: concept_view against indexed_synth_py
# ---------------------------------------------------------------------------
class TestConceptViewBasic:
    async def test_finds_file_by_content_keyword(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        result = await concept_view(session, repo_name, "email")

        assert isinstance(result, ConceptResult)
        assert result.total_matches >= 1
        # utils.py and models.py both mention "email"
        paths = [m.file_path for m in result.matches]
        assert any("utils.py" in p for p in paths)

    async def test_finds_file_by_function_name(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        result = await concept_view(session, repo_name, "format_name")

        assert result.total_matches >= 1
        paths = [m.file_path for m in result.matches]
        assert any("utils.py" in p for p in paths)

    async def test_finds_file_by_class_name(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        result = await concept_view(session, repo_name, "User")

        assert result.total_matches >= 1
        paths = [m.file_path for m in result.matches]
        assert any("models.py" in p for p in paths)

    async def test_no_match_returns_empty(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        result = await concept_view(
            session, repo_name, "xyznonexistent"
        )
        assert result.total_matches == 0
        assert result.matches == []


class TestConceptViewRanking:
    async def test_results_ranked_by_relevance(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """Files with more occurrences of the term should rank higher."""
        session, repo_name = indexed_synth_py
        result = await concept_view(session, repo_name, "email")

        assert len(result.matches) >= 1
        # Results should be in descending rank order.
        ranks = [m.rank for m in result.matches]
        assert ranks == sorted(ranks, reverse=True)


class TestConceptViewSymbolMatching:
    async def test_matching_symbols_populated(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        result = await concept_view(session, repo_name, "validate")

        # utils.py has validate_email — should appear in matching_symbols
        utils_match = next(
            (m for m in result.matches if "utils.py" in m.file_path),
            None,
        )
        assert utils_match is not None
        matching_names = [s.name for s in utils_match.matching_symbols]
        assert "validate_email" in matching_names


class TestConceptViewHeadline:
    async def test_headline_contains_query_term(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        result = await concept_view(session, repo_name, "email")

        assert result.matches
        # The headline should highlight the matched term.
        headline = result.matches[0].headline
        assert "email" in headline.lower()


class TestConceptViewLimit:
    async def test_limit_caps_results(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        result = await concept_view(session, repo_name, "def", limit=1)

        assert len(result.matches) <= 1
        # total_matches may be higher
        assert result.total_matches >= 1


class TestConceptViewSymbolBoost:
    async def test_symbol_match_boosts_rank(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """A file that DEFINES a symbol matching the query should rank
        higher than one that merely mentions the term in content."""
        session, repo_name = indexed_synth_py
        result = await concept_view(session, repo_name, "validate")

        # utils.py defines validate_email — it should rank above files
        # that only mention "validate" in passing (if any).
        assert result.matches
        top = result.matches[0]
        assert "utils.py" in top.file_path
        assert any(s.name == "validate_email" for s in top.matching_symbols)
        # The boosted rank should be higher than the raw FTS rank.
        # (FTS rank for a short file is typically < 0.5; boost adds 0.3)
        assert top.rank > 0.1

    async def test_results_re_sorted_after_boost(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """After symbol boosting, the results should still be in
        descending rank order."""
        session, repo_name = indexed_synth_py
        result = await concept_view(session, repo_name, "name")

        ranks = [m.rank for m in result.matches]
        assert ranks == sorted(ranks, reverse=True)


# ---------------------------------------------------------------------------
# Hybrid mode: FTS + vector similarity
# ---------------------------------------------------------------------------
import pytest_asyncio  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from gita.indexer.embeddings import FakeEmbeddingClient  # noqa: E402
from gita.indexer.ingest import index_repository as _index_repository  # noqa: E402


_SYNTH_REPO_PATH = (
    _Path(__file__).parent.parent / "fixtures" / "synthetic_py"
).resolve()


@pytest_asyncio.fixture
async def indexed_synth_py_with_embeddings(
    db_session: AsyncSession,
):
    """Index synthetic_py with FakeEmbeddingClient so the embedding column
    is populated. Mirrors ``indexed_synth_py`` but enables the hybrid path.
    """
    await _index_repository(
        db_session,
        "synthetic_py",
        _SYNTH_REPO_PATH,
        embedding_client=FakeEmbeddingClient(),
    )
    await db_session.commit()
    yield db_session, "synthetic_py"


class TestConceptViewMode:
    async def test_defaults_to_fts_mode(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        session, repo_name = indexed_synth_py
        result = await concept_view(session, repo_name, "email")
        assert result.mode == "fts"

    async def test_hybrid_without_client_still_fts(
        self,
        indexed_synth_py_with_embeddings: tuple[AsyncSession, str],
    ):
        """Even when embeddings exist, omitting the client keeps FTS mode."""
        session, repo_name = indexed_synth_py_with_embeddings
        result = await concept_view(session, repo_name, "email")
        assert result.mode == "fts"

    async def test_hybrid_mode_activates_with_client_and_embeddings(
        self,
        indexed_synth_py_with_embeddings: tuple[AsyncSession, str],
    ):
        session, repo_name = indexed_synth_py_with_embeddings
        result = await concept_view(
            session,
            repo_name,
            "email",
            embedding_client=FakeEmbeddingClient(),
        )
        assert result.mode == "hybrid"

    async def test_hybrid_falls_back_when_no_embeddings_populated(
        self, indexed_synth_py: tuple[AsyncSession, str]
    ):
        """Client provided but the repo has no embeddings → FTS mode."""
        session, repo_name = indexed_synth_py
        result = await concept_view(
            session,
            repo_name,
            "email",
            embedding_client=FakeEmbeddingClient(),
        )
        assert result.mode == "fts"


class TestConceptViewHybridResults:
    async def test_hybrid_still_finds_keyword_matches(
        self,
        indexed_synth_py_with_embeddings: tuple[AsyncSession, str],
    ):
        """Hybrid mode should not lose FTS hits."""
        session, repo_name = indexed_synth_py_with_embeddings
        result = await concept_view(
            session,
            repo_name,
            "email",
            embedding_client=FakeEmbeddingClient(),
        )
        paths = [m.file_path for m in result.matches]
        assert any("utils.py" in p for p in paths)

    async def test_hybrid_ranks_descending(
        self,
        indexed_synth_py_with_embeddings: tuple[AsyncSession, str],
    ):
        session, repo_name = indexed_synth_py_with_embeddings
        result = await concept_view(
            session,
            repo_name,
            "User",
            embedding_client=FakeEmbeddingClient(),
        )
        ranks = [m.rank for m in result.matches]
        assert ranks == sorted(ranks, reverse=True)

    async def test_hybrid_nonexistent_query_returns_empty(
        self,
        indexed_synth_py_with_embeddings: tuple[AsyncSession, str],
    ):
        """Even in hybrid mode, a truly unrelated query should return
        nothing. FakeEmbeddingClient produces hash-based vectors so the
        distance to any file is effectively random — the strict
        ``_SEMANTIC_DISTANCE_MAX`` guard filters it out."""
        session, repo_name = indexed_synth_py_with_embeddings
        result = await concept_view(
            session,
            repo_name,
            "xyznonexistent",
            embedding_client=FakeEmbeddingClient(),
        )
        assert result.matches == []
        assert result.total_matches == 0

    async def test_query_embedded_once_per_call(
        self,
        indexed_synth_py_with_embeddings: tuple[AsyncSession, str],
    ):
        """The query text is embedded exactly once regardless of how
        many files are being compared against."""
        session, repo_name = indexed_synth_py_with_embeddings
        client = FakeEmbeddingClient()
        await concept_view(
            session, repo_name, "user model", embedding_client=client
        )
        assert client.call_count == 1
        assert client.total_texts == 1
