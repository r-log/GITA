# GITA — GitHub Assistant

GITA is a GitHub App that reviews pull requests and onboards contributors to unfamiliar repositories. It indexes a codebase into a structured database, builds context-aware prompts grounded in that index, runs an LLM against guardrails, and gates any mutation to GitHub behind a trust model.

It is designed to act on signals it can defend — every finding it produces is tied back to a file and line that exist in the index, and every action it takes is either logged, downgraded, or executed based on an explicit write-mode.

## What it does

- **PR review** — on `pull_request.opened` / `synchronize`, GITA pulls the diff, builds per-file context (changed hunks, surrounding symbols, reverse-dependency edges, file body), asks the LLM for findings, verifies each finding against the real AST, and posts a single review comment with a verdict (`approve` / `comment` / `request_changes`).
- **Repo onboarding** — on `issues.opened` with the onboarding label, GITA produces a project summary, a set of cited findings, and milestone groupings to help a new contributor orient.
- **Concept search** — `gita query concept <repo> <query>` runs full-text search over the indexed code with symbol-name boosting, returning ranked files and highlighted snippets. The result shape is designed to swap to embeddings without changing callers.
- **Incremental indexing** — on `push`, GITA re-indexes only the files that changed since the last known SHA, falling back to a full rebuild when git state is ambiguous.

## Architecture

```
GitHub ──webhook──► FastAPI receiver ──► dispatch ──► ARQ (Redis) ──► runner
                         │                                                │
                         └── HMAC + bot-filter + cooldown + job-dedup     │
                                                                          ▼
                                                        ┌─── indexer (Tree-sitter → Postgres)
                                                        ├─── views (diff context, concept search)
                                                        ├─── LLM (OpenRouter, schema-validated)
                                                        ├─── guardrails (AST verification)
                                                        └─── bridge → decision → GitHub API
```

- **Webhook layer** (`src/gita/web/`) enforces four walls before anything is enqueued: HMAC signature, bot-sender filter (loop prevention), event allowlist, per-repo cooldown. ARQ's `_job_id` deduplication is the fifth wall.
- **Indexer** (`src/gita/indexer/`) parses source files with Tree-sitter and writes to `code_index` (files, content, symbols) and `import_edges` (import graph). Incremental mode routes through `git diff --name-status` and only touches affected rows.
- **Views** (`src/gita/views/`) are deterministic query layers. Agents never hit the DB directly — they compose views. This keeps prompts reproducible and makes the agent logic testable without Postgres.
- **Agents** (`src/gita/agents/`) are recipes: a fixed sequence of view calls and LLM calls, each with a Pydantic response schema. There are no free-form agent loops.
- **LLM client** (`src/gita/llm/`) is a thin OpenRouter wrapper. Every call that returns structured output is validated against a Pydantic schema; schema failures raise `LLMSchemaError` rather than degrading silently.
- **Guardrails** (`src/gita/agents/guardrails.py`) re-parse the cited file and reject findings whose file, line, or symbol don't exist. Findings that survive contribute to a structural confidence score blended with the LLM's self-reported confidence.
- **Decision layer** (`src/gita/agents/decisions.py`) converts an agent result into a proposed GitHub action, then routes it through `WRITE_MODE` before executing.

## Trust model — `WRITE_MODE`

Every action an agent wants to take passes through a single gate:

| Mode | Behavior |
|---|---|
| `shadow` | Log the proposed action. Never touch GitHub. Default. |
| `comment` | Execute comment-type actions if confidence clears threshold. Downgrade everything else (labels, approvals, requested changes) to a comment. |
| `full` | Execute any action that clears its own confidence threshold. |

The mode is set by environment, not by the agent. An agent cannot elevate its own privileges. Every executed action is recorded in `agent_actions` with its evidence, so re-runs are idempotent and audits are exact.

## Data model

- `repos` — tracked repositories, keyed by `full_name`.
- `code_index` — one row per source file: path, language, symbols (JSONB), content, last-indexed SHA. GIN index on `to_tsvector('simple', content)` backs concept search.
- `import_edges` — directed edges between files in the same repo, used for reverse-dependency lookup during PR context building.
- `agent_actions` — append-only log of every decision an agent made and what was (or wasn't) executed. Used for SHA-based review dedup and for audit.

## Testing

Two suites run under `pytest`:

- **Non-LLM suite** — 520+ tests covering indexer, views, guardrails, webhook gates, bridges, decision routing. Uses `FakeLLMClient` with canned responses. No network.
- **Gated golden suite** — a smaller set that hits a real LLM for prompt regression. Opt-in via env flag.

Integration tests that touch git or Postgres spin up a real tmpdir git repo and a real transactional DB session — never mocks.

## Repository layout

```
src/gita/
  agents/         recipes, guardrails, decisions, bridges
  cli/            `gita` commands (index, query, review, onboard)
  db/             SQLAlchemy models and session factory
  github/         App auth (JWT → installation token), API client
  indexer/        parsers, ingest, incremental diff
  jobs/           runners shared by CLI and ARQ worker
  llm/            OpenRouter client, schemas, protocol
  views/          deterministic query layer
  web/            FastAPI webhook receiver, dispatch, cooldown
  worker.py       ARQ worker entry point
alembic/          migrations
tests/            pytest suites
```

## Status

The PR reviewer and onboarding agents are shipped. Webhook + ARQ plumbing is in place. Write mode defaults to `shadow`; it is flipped to `comment` or `full` deliberately for live runs and flipped back. Embedding-backed concept search and branch-creation / test-generation are on the roadmap.
