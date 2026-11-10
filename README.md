# GITA: repository memory for coding agents

GITA remembers what happened in a repository: every merged pull request,
and which later fixes repaired which earlier PRs, traced line by line
through git. It gives that memory to coding agents over
[MCP](https://modelcontextprotocol.io) while they work, and keeps a
**scoreboard** of how often merged PRs later needed a fix, and where.

None of it calls a model. It is built from git history and a Tree-sitter
code index, so it is fast, free to query, and the same answer every time.

## Why

Coding agents now write a change and review it in the same session. That
review is good, but it has two structural gaps:

- **No memory across sessions.** Every session starts fresh. It doesn't
  know this repository has already shipped, and fixed, the mistake it is
  about to make.
- **No measurement.** Nobody knows how often agent-reviewed PRs still
  ship bugs, in which areas, or whether a new model or workflow improves
  that.

Measured on [r-log/mangosthree-server](docs/scoreboard-mangosthree-server.md),
a C++ game server developed with coding agents (725 PRs since June 2026):
19 merged PRs later needed a fix, and the same mistake came back. In
`persistence/characters`, three separate PRs (#145, #411, #415) deleted
every character's rows where they meant the loading character's; each
needed its own fix.

## Quick start

```bash
gita history sync ~/src/my-repo --name my-repo   # git only; ~1 min for 700 PRs
gita index ~/src/my-repo --name my-repo          # code index, for impact()
cd ~/src/my-repo && claude mcp add gita -- gita mcp
gita scoreboard my-repo --since 2026-06-01
```

Once a repository has been synced, the worker keeps its memory current on
every push to the default branch (incremental: about a second).

## What an agent can ask

| Tool | Answers |
|---|---|
| `lessons(paths)` | What past fixes corrected in these files, then their directory and its parents |
| `past_bugs(path or text)` | Defects fixed in an area: the PR that introduced each, how long it lived, the exact lines |
| `review_context(diff)` | One call before finishing a change: past bugs, lessons, recent PRs and dependents of every touched file |
| `impact(file or symbol)` | What includes or imports a file; where a symbol is defined (signature, doc) and which files mention it |
| `history(path)` | Merged PRs that touched a file, by kind, and how many traced defects sit in it |
| `scoreboard(since)` | How often merged PRs later needed a fix, by kind, size, area and month |

The server's instructions tell the agent when to call each. A real answer,
for an agent about to edit mangos's `CurrencyRepository.cpp`:

```
lessons(["src/game/persistence/characters/inventory/CurrencyRepository.cpp"])
  same file:        "Delete only the logging-in character's row of an unknown currency"  (#468 fixed #411)
  parent directory: "Read and keep all eight guild bank tabs of a guild member"          (#507 fixed #451)
  parent directory: "Delete a refused spell, criteria or pet spell for the loading character only" (#485 fixed #145)
  parent directory: "Delete only the logging-in character's invalid talent and glyph rows" (#465 fixed #415)
```

## The scoreboard

`gita scoreboard mangosthree-server --since 2026-06-01` (full report:
[docs/scoreboard-mangosthree-server.md](docs/scoreboard-mangosthree-server.md)):

| | Later needed a fix |
|---|---|
| All merged PRs | 2.6% (19/725) |
| Features / refactors / fixes | 1.9% / 4.3% / 4.4% |
| Under 50 added lines / 500+ | 0% / 3.8% |
| Days until fixed | median 2, 90th percentile 48 |

## How it works

`gita history sync` reads the default branch's first-parent history:

1. **Merged PRs**: GitHub merge commits and squash merges ("Title (#N)").
   A PR's own changes run from its merge base to its head, as GitHub's
   "Files changed" shows them. Each gets a kind (fix, feature, refactor,
   docs, test, chore) from its branch prefix or a conventional title
   prefix. In a fork, the upstream's older squash commits carry the
   upstream's PR numbers and are treated as inherited history.
2. **Defect links**: for each fix PR, the product-code lines it modified
   or removed are blamed at its merge base (an insertion blames the lines
   around it) and traced to the PRs that wrote them, with each PR's share
   of the lines and where they sat in its head, following renames. This
   is the SZZ approach: strong evidence, not proof. A refactor that moved
   buggy code is credited with it, so every answer carries the lines.
3. **Incremental**: later syncs read only new merges, and start over if
   the default branch's history was rewritten.

The tracing was validated against the hand-checked ground truth of the
backtest below: all 17 defects reproduced exactly (PR pairs, files, lines
and shares).

## Also in GITA

GITA is also a GitHub App, with agents that use the same index:

- **PR reviewer** (on pull request events). It reads each changed file at
  the PR head, routes the review to a model tier by what the PR changes,
  checks every finding against the code the model was shown, computes the
  verdict in code, and posts one comment. Measured
  ([evals/pr_review](evals/pr_review/README.md),
  [evals/pr_backtest](evals/pr_backtest/)): on 16 real defects that had
  slipped into mangos, Claude Haiku 5.5 found 5 at $0.007 a review, as
  many as Claude Fable 5.2 did at $0.73; no model found the rest. That
  result is why GITA's focus moved to memory: a reviewer catches some
  bugs, and the repository's own history explains the rest.
- **Onboarding** (when a maintainer applies `gita:onboard` to an issue):
  a project summary, cited findings and milestones.
- **Test generation** (opt-in per repo): after a push adds a module
  without tests, it writes a pytest file, runs it in an isolated
  container, and opens a PR only if the tests pass.
- **Indexing** of Python, TypeScript/TSX, JavaScript, C, C++ and C#:
  symbols, signatures, docstrings, an import/include graph, full-text
  search and optional embeddings. A 2,100-file C++ server indexes in about
  11 s, with 99.9% of its project `#include`s resolved without the build
  system.

## Design

- **Code decides, models suggest.** Memory, scoreboard and impact are
  computed, never generated. Where a model is used (the reviewer), its
  output is untrusted input: guardrails check every citation against
  ground truth (`agents/guardrails.py`), and the verdict and confidence
  come from evidence.
- **Recipes, not agent loops.** Each agent is a fixed sequence of
  deterministic views and schema-validated model calls.
- **One write gate.** Every side effect is a `Decision` routed through
  `execute_decision` (`agents/decisions.py`), reserved in `agent_actions`
  before the GitHub call so retries and concurrent workers act once.
- **Untrusted code runs in a sandbox.** Generated tests execute in a
  container with no network, no host environment and a read-only
  workspace (`agents/test_generator/sandbox.py`).

The threat model, with the control for each threat and the known gaps,
is in [SECURITY.md](SECURITY.md).

### Trust model: `WRITE_MODE`

| Mode | Behaviour |
|---|---|
| `shadow` (default) | Log and record what would happen. Never call GitHub. |
| `comment` | Post comments that clear their threshold; any other action becomes a comment explaining what was intended. |
| `full` | Execute any action that clears its threshold. |

The memory and the MCP server only read; `WRITE_MODE` governs the agents
that post.

## Running it

Requirements: Python 3.12+, Docker, git.

```bash
docker compose up -d db redis                  # Postgres (pgvector) on :5433, Redis on :6379
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                           # fill in keys; WRITE_MODE stays shadow
alembic upgrade head

# Memory and index for a dedicated clone with history (not shallow, ideally)
gita history sync /srv/gita/checkouts/my-repo --name my-repo
gita index /srv/gita/checkouts/my-repo --name my-repo --github owner/my-repo

# For agents, and for people
gita mcp --repo my-repo
gita scoreboard my-repo

# The GitHub App: webhooks and the worker
uvicorn gita.web:app --port 8000
arq gita.worker.WorkerSettings
```

The `Dockerfile` builds one image for both the web server and the
worker.

### Configuration

| Variable | Purpose |
|---|---|
| `DATABASE_URL`, `REDIS_URL` | Postgres (with pgvector) and Redis (`rediss://` for TLS). |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_WEBHOOK_SECRET` | GitHub App credentials. |
| `GITHUB_APP_SLUG` | Ignore only GITA's own bot; other bots' PRs are reviewed. |
| `OPENROUTER_API_KEY`, `AI_DEFAULT_MODEL` | LLM provider and model (the reviewer and other agents; the memory needs neither). |
| `AI_MODEL_SMALL`, `AI_MODEL_STANDARD`, `AI_MODEL_LARGE` | Optional model tiers: the reviewer routes each PR to one by what it changes (`agents/routing.py`). |
| `OPENAI_API_KEY` | Optional: file embeddings for semantic search. |
| `WRITE_MODE` | `shadow` / `comment` / `full`. |
| `ONBOARDING_LABEL` | Label that requests onboarding (default `gita:onboard`). |
| `REVIEW_DEBOUNCE_SECONDS` | Wait before reviewing, so bursts of pushes yield one review. |
| `TEST_GEN_SANDBOX`, `TEST_GEN_SANDBOX_IMAGE` | `docker` (required for webhook-triggered runs) or `local` (trusted code only). |
| `AUTO_TEST_GEN_ENABLED`, `AUTO_TEST_GEN_MAX_PER_REINDEX` | Global switch and cap for automatic test generation. |

See `.env.example` for the full list.

## Testing

```bash
DATABASE_URL=postgresql+asyncpg://github_assistant:password@localhost:5433/github_assistant \
  pytest
```

About 1,100 tests run against a real Postgres whose schema is built by
the Alembic migrations, and against real git repositories rather than
mocks. Highlights:

- history tracing on repositories built with real git commands (merge
  commits, squash merges, fixes to tests only, inherited upstream
  commits, rewritten history);
- every MCP tool called through an MCP client, and the server started
  over stdio as an agent would;
- a replay of a realistic webhook delivery log asserting exactly which
  jobs are enqueued, and concurrent Postgres transactions proving two
  workers post one comment;
- sandbox tests that run real containers when Docker and the sandbox
  image are available.

Evals that call real models are separate ([evals/](evals/)): a synthetic
PR-review suite with replay of recorded answers, and a backtest on real
PRs that a later PR had to fix. CI (`.github/workflows/ci.yml`) runs lint,
formatting, the full suite, `alembic check`, and an image build.

## Known limitations

- **Defect links are evidence, not proof** (line tracing credits a
  refactor with the bugs it moved), and only fix PRs are seen: a fix
  pushed without a PR, or one not named as a fix, is invisible.
- **The memory reads one branch's history.** A shallow clone records
  older changes as inherited code; sync from a full clone.
- **Lessons are fix titles.** They are only as clear as the repository's
  fix PRs are named.
- **Review comments are a single PR comment**, not inline review
  comments, and **token permissions** are scoped to one repository but
  not narrowed per action (see SECURITY.md).
- **Indexing:** some TypeScript forms are not captured; C/C++ `#include`s
  are resolved by path rather than the build's include directories, and
  macros aren't expanded; C# `using` directives are not resolved.
- **Test generation** targets Python only.

## Repository layout

```
src/gita/
  history/       repository memory: merged PRs, defect links, queries, scoreboard
  mcp_server.py  the memory over MCP
  agents/        recipes, guardrails, decision gate, idempotency, routing, rendering
    pr_reviewer/     diff parsing, review recipe, comment bridge
    onboarding/      onboarding recipe and bridges
    test_generator/  preflight gates, recipe, sandbox, bridge
    prompts/         system prompts
  cli/           `gita` command
  db/            SQLAlchemy models and session
  github/        App auth (JWT → repo-scoped installation tokens) and API client
  indexer/       walker, Tree-sitter parsers, import resolution, incremental diff, embeddings
  jobs/          ARQ jobs, job IDs, retry policy, runners shared with the CLI
  llm/           LLM client protocol, OpenRouter client, fake for tests
  views/         deterministic query layer used by the agents
  web/           FastAPI webhook receiver and event dispatch
alembic/         migrations (the schema's source of truth, checked in CI)
evals/           PR-review evals: synthetic suite and real-PR backtest, with recorded runs
docs/            generated reports
docker/          sandbox image for generated tests
tests/           pytest suites and fixture repositories
```
