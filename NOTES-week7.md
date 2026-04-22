# GITA v2 — Week 7 notes

Working notes captured at the end of Week 7. Pairs with [NOTES.md](NOTES.md) (Week 1), [NOTES-week2.md](NOTES-week2.md) (Week 2), [NOTES-week3.md](NOTES-week3.md) (Week 3), [NOTES-week4.md](NOTES-week4.md) (Week 4), [NOTES-week5.md](NOTES-week5.md) (Week 5), and [NOTES-week6.md](NOTES-week6.md) (Week 6).

---

## Status

**Week 7 acceptance bar met.** Indexed code now carries both symbol-level metadata (signatures, docstrings, decorators for Python; signatures and JSDoc for TS/JS) and file-level semantic embeddings. `concept_view` runs as a hybrid search that combines Postgres FTS with pgvector cosine similarity when embeddings are available, and transparently falls back to the legacy keyword path otherwise.

| | |
|---|---|
| Full suite (no LLM) | **691 passed** in ~1m 42s |
| Alembic migrations | 5 (initial + agent_actions + FTS index + github_full_name + embeddings) |
| New Postgres image | `pgvector/pgvector:pg16` |
| `WRITE_MODE` at EOD | `shadow` (unchanged) |

---

## What Week 7 shipped

### Day 1 — Python symbol enrichment

Enriched the Python parser with call-site-useful metadata on every function and class symbol:
- `signature` — `def foo(x: int) -> bool` style strings (async prefix + return type)
- `docstring` — first line of the function/class docstring, capped at 200 chars
- `decorators` — list of decorator strings harvested from `decorated_definition` parents

[src/gita/indexer/parsers.py](src/gita/indexer/parsers.py) — new `_extract_python_signature`, `_extract_python_docstring`, `_extract_decorators` helpers, wired into `_extract_python` for functions and classes.

Commit `541ae4f`. **+12 parser tests.**

### Day 2 — TS/JS symbol enrichment

Brought TS/JS parity to Python. `_extract_ts_js_signature` builds signature strings (including `async` prefix and return type annotations); `_extract_jsdoc` pulls the leading block comment. Applied to functions, classes, interfaces, and methods, including those wrapped in `export_statement` or `export_default_declaration`.

Commit `f87eb68`. **+14 parser tests.**

### Day 3 — pgvector infrastructure

Added the DB/infra for file-level embeddings without wiring it into ingest yet:
- Docker image swapped to `pgvector/pgvector:pg16`
- `pgvector>=0.3` and `openai>=1.0` added to `pyproject.toml`
- `OPENAI_API_KEY` added as an optional `Settings` field ([src/gita/config.py](src/gita/config.py))
- Nullable `embedding Vector(1536)` column on `CodeIndex`, guarded by an import-time try/except so the model is importable without pgvector installed ([src/gita/db/models.py](src/gita/db/models.py))
- Alembic migration 0005 — enables the `vector` extension, adds the column, creates an HNSW cosine index
- Test DB bootstrap in [tests/conftest.py](tests/conftest.py) runs `CREATE EXTENSION IF NOT EXISTS vector` before `create_all`
- [src/gita/indexer/embeddings.py](src/gita/indexer/embeddings.py) — `EmbeddingClient` Protocol, `OpenAIEmbeddingClient` (real), `FakeEmbeddingClient` (deterministic hash-based vectors for tests)

Commit `f6084f7`. **+8 embedding tests.**

### Day 4 — Wire embeddings into ingest

Took the infrastructure from Day 3 and made ingest actually populate the `embedding` column.

- [src/gita/indexer/embeddings.py](src/gita/indexer/embeddings.py) — new `make_embedding_client()` factory: returns an `OpenAIEmbeddingClient` when `settings.openai_api_key` is set, else `None`. Also new `prepare_embedding_input()` helper that truncates to `EMBEDDING_INPUT_CHAR_LIMIT` (8000 chars) so we don't ship multi-megabyte files to the API.
- [src/gita/indexer/ingest.py](src/gita/indexer/ingest.py) — added `_attach_embeddings()` helper that batches every row's content into a single `client.embed` call, then assigns `row.embedding`. `index_repository`, `_full_index`, and `_incremental_index` all accept an `embedding_client` kwarg (default `None` = NULL embeddings = existing behaviour).
- `IngestResult` grew a `files_embedded: int` field.
- Empty files are skipped (embedding stays NULL); truncation is applied before the API call.
- [src/gita/cli/commands.py](src/gita/cli/commands.py) — `cmd_index` calls `make_embedding_client()` and passes it through, closing the client in a `finally` block.
- [src/gita/jobs/runners.py](src/gita/jobs/runners.py) — `run_reindex_job()` does the same, and the job result dict now includes `files_embedded`.
- [src/gita/cli/formatters.py](src/gita/cli/formatters.py) — `fmt_ingest` prints an `embedded:` line when any files were embedded.

**14 new tests** (3 factory + 11 ingest-embedding).

Key contract: **when no API key is configured, embedding is a no-op.** The column stays NULL, no API calls are made, and `concept_view` silently falls back to FTS-only. This means every existing test, fixture, and development workflow continues to work with zero config.

### Day 5 — Hybrid FTS + semantic `concept_view`

Upgraded `concept_view` to combine keyword and semantic search.

- [src/gita/views/concept.py](src/gita/views/concept.py) — rewrite around three helpers: `_fts_candidates`, `_semantic_candidates`, `_repo_has_embeddings`.
- When an `embedding_client` is passed AND the repo has at least one populated embedding, the query text is embedded (once), a cosine-distance search pulls the top candidates, results are merged with the FTS candidates by `file_path`, and each file's final rank is `w_fts * normalized_fts + w_vector * similarity + symbol_boost`. Weights are 0.5/0.5 as a starting point.
- A strict `_SEMANTIC_DISTANCE_MAX = 0.5` cutoff filters noise — nonsense queries like `"xyznonexistent"` still return zero matches even though some file is always "closest" in vector space.
- `ConceptResult.mode` field distinguishes `"fts"` vs `"hybrid"` so callers (and tests) can tell which path ran.
- Legacy behaviour is preserved: no client, or client without populated embeddings → pure FTS with the existing symbol boost.
- [src/gita/cli/commands.py](src/gita/cli/commands.py) — `cmd_query_concept` now builds the client via `make_embedding_client()` and passes it through.

**8 new concept tests** (mode selection, hybrid result shape, nonexistent-query filter, single-call embedding).

### Day 6 — Notes, full-suite verification

- Wrote NOTES-week7.md (this file)
- Confirmed `691 passed` in the non-LLM suite (up from 654 at end of Week 6 → +37 tests across the week)

---

## Test counts

| Layer | Week 6 | Week 7 | Total |
|---|---|---|---|
| Indexer parsers (signatures/docstrings/JSDoc) | ~50 | +26 | ~76 |
| Indexer embeddings (client + factory) | 0 | +11 | 11 |
| Indexer ingest (embedding wiring) | ~30 | +11 | ~41 |
| Views concept (hybrid mode) | 15 | +8 | 23 |
| All other layers | unchanged | 0 | ~540 |
| **Total (non-LLM)** | **654** | **+37** | **691** |

---

## Architecture changes

### Embedding contract

A single factory, three touchpoints, one rule:

```
make_embedding_client() → OpenAIEmbeddingClient | None
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
    cmd_index          run_reindex_job       cmd_query_concept
          │                    │                    │
          └────── index_repository(..., ─┘          │
                  embedding_client=X)               │
                          │                         │
                          ▼                         ▼
                  code_index.embedding    concept_view(...,
                          │               embedding_client=X)
                          │                         │
                          └──────── hybrid ◄────────┘
```

**Rule: `None` is always valid.** If any piece of the chain is missing (no API key, no populated embeddings, embedding call failure), the system degrades cleanly to the keyword-only path. Tests exploit this: every existing test keeps `embedding_client=None` and continues to assert against FTS behaviour.

### Hybrid ranking

For files present in both candidate sets:

```
final_rank = 0.5 * (fts_rank_cd / max_fts_rank)     # normalized FTS
           + 0.5 * (1 - cosine_distance / 2)         # similarity in [0, 1]
           + min(matching_symbols * 0.3, 1.0)        # symbol-name boost (unchanged)
```

For semantic-only hits, the FTS term is zero (file isn't in the tsquery candidate list); for FTS-only hits, the vector term is zero. A file below `_SEMANTIC_DISTANCE_MAX = 0.5` in the semantic query is dropped entirely — this is what keeps nonsense queries from returning anything.

The strict distance cutoff is doing a lot of work. For production OpenAI embeddings on unit-norm vectors, relevant code is usually well under 0.3 cosine distance. 0.5 is the permissive end of the range; it can be tightened if drift becomes a problem.

### Full vs incremental embedding

- **Full index:** every non-empty file gets re-embedded. This is expensive on first index of a large repo but matches the "nuke and repave" semantics of full mode.
- **Incremental index:** only the files that the git-diff pipeline identifies as added/modified are re-embedded. Deleted files lose their row; unchanged files keep whatever embedding they had. This is the steady-state path for webhook-driven re-indexes.

---

## Known limitations / follow-ups

- **Cache by content hash.** Full re-indexes re-embed every file even if the content is byte-identical to what was embedded last time. A content-hash column on `CodeIndex` would let us skip the embed call. Adds one schema column and one lookup; worth doing once we have real repos at scale.
- **Query-side cache.** The query text is embedded on every `concept_view` call. At scale we'd want a small in-process LRU keyed on `(query, model)`.
- **Symbol-level embeddings.** We currently embed whole-file content. For very large files the signal gets diluted. A follow-up is to embed per-symbol using the Day 1/2 signatures + docstrings and store them in a separate table.
- **Hybrid weights are not tuned.** 50/50 is a reasonable default but the right mix is data-dependent. Need a small benchmark harness with labeled query → expected-file pairs before we tune.

---

## What comes next (Week 8)

Two candidates in priority order:

1. **Branch creation + test generation.** User's requested future capability. GITA goes from commenter to contributor — opens a branch, writes a patch, opens a PR. Requires Contents API integration, a new trust/write-mode dimension, and a much stricter confidence gate.
2. **Exponential backoff for LLM + embedding failures.** Still fixed 30s retry in the worker. Both the LLM and the embedding API benefit from smarter retries; the embedding path especially, since re-indexing is write-heavy and partial failures should be survivable.

---

## Current WRITE_MODE: shadow
