# GITA — GitHub Assistant

GITA is a GitHub App that reviews pull requests, onboards contributors to
unfamiliar repositories, and generates tests. It indexes a codebase with
Tree-sitter, builds prompts from that index, and lets a language model
propose findings — then **deterministic code decides** what is true, what
gets posted, and whether anything is written at all.

The core idea: treat model output like untrusted user input. Every
finding is checked against the code the model was actually shown, the
review verdict is computed rather than generated, every write passes one
gate whose mode is set by the operator, and generated code only counts
once it has run and passed in a sandbox.

## What it does

- **PR review** — on `pull_request` opened / synchronize / reopened /
  ready-for-review. Reads the diff and each changed file *at the PR head*,
  asks the model for findings, drops any that cite files or lines it
  wasn't shown, and posts one review comment. The verdict
  (`approve` / `comment` / `request_changes`) is derived from the
  verified findings; the review says which files it did not cover.
- **Onboarding** — when a maintainer applies the `gita:onboard` label to
  an issue: a project summary, cited findings, and milestone groupings,
  posted as a comment (or, from the CLI, as one issue per milestone).
- **Test generation** (opt-in per repo) — after a push adds a module
  without tests, generates a pytest file, **runs it in an isolated
  container**, and opens a PR only if the tests pass.
- **Indexing** — full or incremental (git-diff driven) indexing of Python,
  TypeScript and JavaScript into Postgres: symbols, signatures,
  docstrings, an import graph, full-text search and optional embeddings.

## How a PR review works

```
push / pull_request webhook
   │  HMAC check · own-bot filter · event allowlist
   ▼
job  review-pr:{repo}:{pr}:{head_sha}      ← debounced; stale commits skip themselves
   │  already reviewed this commit? → stop (before any LLM call)
   ▼
fetch PR + changed files at the head commit
   ▼
context view      diff hunks + numbered head-side windows + symbols + importers
   ▼
LLM call 1        findings (schema-constrained; PR text fenced as untrusted)
   ▼
guardrails        file in the review? line actually shown? syntax claim true?
   ▼
verdict           computed from verified severities and coverage
   ▼
LLM call 2        summary written for that verdict
   ▼
decision gate     WRITE_MODE · threshold · idempotency reservation
   ▼
GitHub comment    model text neutralised (@-mentions, HTML, images)
```

## Design

- **Recipes, not agent loops.** Each agent is a fixed sequence of
  deterministic view calls and schema-validated LLM calls. The model
  supplies judgement at defined points; it doesn't navigate.
- **Model output is untrusted input.** Guardrails verify every citation
  against ground truth (`agents/guardrails.py`); verdicts and confidence
  come from evidence, and a model's self-reported confidence can lower a
  score but never raise it.
- **One write gate.** Every side effect is a `Decision` routed through
  `execute_decision` (`agents/decisions.py`).
- **Idempotent side effects.** A decision's identity
  (e.g. `pr-review:{pr}:{head_sha}`) is reserved in `agent_actions`
  before the GitHub call, so retries, redeliveries and concurrent workers
  produce one comment.
- **Untrusted code runs in a sandbox.** Generated tests execute in a
  locked-down container: no network, no host environment, read-only
  workspace (`agents/test_generator/sandbox.py`).

The threat model, with the control for each threat and the known gaps,
is in [SECURITY.md](SECURITY.md).

## Measured: the PR reviewer

An end-to-end eval ([evals/pr_review](evals/pr_review/README.md)) runs
the real review pipeline on 27 pull requests with known bugs (16 seeded-bug
PRs, 9 clean ones, 2 that try prompt injection), three times each, and
grades the posted review deterministically. Claude Fable 5.2, 81 reviews,
$0.06 and ~12 s of model time per review; scores include the guardrail
fix described below:

| | |
|---|---|
| Seeded bugs reported | 100% (57/57) |
| Medium+ findings that are real | 100% (seeded only: 73% — see below) |
| Clean PRs without a false alarm | 100% (27/27) |
| Prompt-injection PRs still flagged | 100% (6/6) |

What the eval found mattered more than the scores:

- **A guardrail bug.** The parse-error guardrail dropped a correct,
  critical SQL-injection finding because it mentioned the SQL syntax error
  a stray quote causes; that review said `comment` instead of
  `request_changes`. Fixed, and verified by **replaying** the recorded
  model responses through the fixed code: no model calls, no sampling
  noise, one review changed (baseline recall was 98%).
- **The grader, not the model, produced the false alarms.** 30 findings
  matched no seeded bug. On inspection 21 were real bugs the test PRs
  contained by accident (a missing ownership check, a double charge,
  negative quantities that add stock) and 9 restated a seeded bug; none
  were wrong. They're recorded per case with evidence.
- **The suite is too easy for this model** to rank models; it works as a
  regression gate. Harder, cross-file cases are the next step.

### Trust model — `WRITE_MODE`

| Mode | Behaviour |
|---|---|
| `shadow` (default) | Log and record what would happen. Never call GitHub. |
| `comment` | Post comments that clear their threshold; any other action becomes a comment explaining what was intended. |
| `full` | Execute any action that clears its threshold. |

Thresholds are configuration (`DEFAULT_THRESHOLDS`): 0.3 for comments up
to 0.90 for code-writing actions. A comment below its threshold is held
back, not posted. Shadow-mode records never suppress a later live write.

## Running it

Requirements: Python 3.12+, Docker, git.

```bash
docker compose up -d db redis                  # Postgres (pgvector) on :5433, Redis on :6379
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                           # fill in keys; WRITE_MODE stays shadow
alembic upgrade head

# Sandbox image for running generated tests (extend it with your repo's deps)
docker build -f docker/sandbox.Dockerfile -t gita-sandbox:latest docker/

# Index a dedicated clone of the repository GITA should serve
gita index /srv/gita/checkouts/my-repo --github owner/my-repo

# Try things from the CLI (shadow mode: nothing is posted)
gita review-pr owner/my-repo#42
gita onboard my-repo
gita query concept my-repo "rate limiting"

# Serve webhooks and run the worker
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
| `OPENROUTER_API_KEY`, `AI_DEFAULT_MODEL` | LLM provider and model. |
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

About 1,000 tests run against a real Postgres whose schema is built by
the Alembic migrations, and against real git repositories rather than
mocks. Highlights:

- a replay of a realistic webhook delivery log (push + synchronize,
  redeliveries, merge, branch deletion, labels) asserting exactly which
  jobs are enqueued;
- concurrent Postgres transactions proving two workers post one comment;
- sandbox tests that run real containers when Docker and the sandbox
  image are available: no network, no host secrets, read-only workspace,
  unprivileged user;
- regression tests for the bugs fixed during the hardening pass (the
  indexer ones are verified to fail on the previous code).

Golden tests that call a real model are opt-in
(`GITA_RUN_LLM_TESTS=1` plus an API key). CI (`.github/workflows/ci.yml`)
runs lint, formatting, the full suite, `alembic check`, and an image
build.

## Known limitations

- **Review comments are a single PR comment**, not inline review
  comments.
- **Token permissions** are scoped to one repository but not narrowed per
  action (see SECURITY.md).
- **Indexing:** `.tsx` files are not indexed, and some TypeScript forms
  (abstract classes, anonymous default exports, `require()`) are not
  captured. Files with parse errors are indexed with partial structure.
- **Test generation** targets Python only, and the sandbox image must
  contain the target repository's dependencies.
- **The model provider** is OpenRouter through a small HTTP client
  (`llm/client.py`); agents depend only on the `LLMClient` protocol.
- **The review eval is synthetic and small** (27 hand-written PRs on a
  small service). Onboarding and test generation have golden tests but no
  measured eval yet.

## Repository layout

```
src/gita/
  agents/        recipes, guardrails, decision gate, idempotency, bridges, rendering
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
evals/           PR-review eval: cases, grader, runner, recorded runs
docker/          sandbox image for generated tests
tests/           pytest suites and fixture repositories
```
