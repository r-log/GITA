<div align="center">

# GITA

### **G**itHub **I**ntelligent **T**racking **A**ssistant

_An AI-powered project assistant that lives inside your GitHub repos._

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)](https://postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)](https://redis.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

---

**GITA monitors your repos in real-time** -- analyzing issues, reviewing PRs, tracking milestones, and catching risks before they become problems. It thinks, decides, and acts -- all through GitHub comments and check runs.

</div>

<br>

## What It Does

> Install GITA on a repo and it starts working immediately. No commands to run. No dashboards to check.

- **Indexes your codebase** -- deterministic AST/grammar parsing for Python, Go, Java, Rust, C#, Ruby, PHP, JS/TS. Understands your routes, models, services, and gaps without burning LLM tokens.
- **Creates a project plan** -- generates milestones and sub-issues based on what's built and what's missing.
- **Keeps the plan current** -- on every push, re-indexes changed files. On re-runs, compares code vs issues and closes completed work, creates new tasks, flags stale items.
- **Evaluates issues** -- scores them against S.M.A.R.T. criteria, checks milestone alignment.
- **Tracks progress** -- calculates velocity, predicts deadlines, detects blockers and stale PRs.
- **Reviews PRs** -- analyzes diff quality, checks test coverage, verifies linked issues.
- **Catches risks** -- scans for leaked secrets, security vulnerabilities, breaking changes, and dependency issues.

<br>

## How It Works

GITA is a **multi-agent system** with a **deterministic code indexer** at its core.

```
GitHub Webhook
  |
  v
Supervisor Agent (classifies event, picks agents)
  |
  +---> Onboarding Agent     -- index repo, create/update issues
  +---> Issue Analyst         -- S.M.A.R.T. evaluation
  +---> Progress Tracker      -- velocity, blockers, deadlines
  +---> PR Reviewer           -- diff quality, test coverage
  +---> Risk Detective        -- secrets, vulnerabilities, breaking changes
```

The **Supervisor** receives every event and decides which agents to dispatch -- often running them **in parallel**. Each agent picks from its own scoped toolset, reasons through the problem, and takes action.

### The Code Indexer

Instead of expensive LLM file-reading, GITA parses your entire codebase deterministically:

| Language       | Parser         | What It Extracts                                                   |
| -------------- | -------------- | ------------------------------------------------------------------ |
| Python         | stdlib `ast`   | imports, classes, functions, decorators, routes, constants         |
| JS/TS          | regex patterns | imports, exports, classes, functions, routes, React components     |
| Go             | tree-sitter    | imports, structs, interfaces, methods, Gin/Echo routes             |
| Java           | tree-sitter    | imports, classes, fields, methods, Spring annotations/routes       |
| Rust           | tree-sitter    | use declarations, structs, enums, impl blocks, Actix/Axum routes   |
| C#             | tree-sitter    | using directives, classes, properties, methods, ASP.NET attributes |
| Ruby           | tree-sitter    | require, classes, methods, Rails/Sinatra routes                    |
| PHP            | tree-sitter    | use/namespace, classes, methods, Laravel routes                    |
| JSON/YAML/TOML | stdlib         | dependencies, scripts, config structure                            |

The result is a compressed **code map** (~3-10KB) that any agent can query -- routes, models, services, gaps, TODOs -- without reading a single file.

The progressive flow produces an **action list** -- close completed issues, create new ones, update tracker checklists, flag stale items -- and executes it without another LLM call.

<br>

## The Agents

| Agent                | Triggers                      | What It Does                                                      |
| -------------------- | ----------------------------- | ----------------------------------------------------------------- |
| **Onboarding**       | App installed, manual trigger | Indexes codebase, creates/updates milestones and issues           |
| **Issue Analyst**    | Issue opened/edited           | S.M.A.R.T. evaluation, milestone alignment, constructive feedback |
| **Progress Tracker** | Milestone events, pushes      | Velocity trends, blocker detection, deadline prediction           |
| **PR Reviewer**      | PR opened/updated             | Diff quality, test coverage, linked issue verification            |
| **Risk Detective**   | PR opened/updated, pushes     | Secret scanning, vulnerability patterns, breaking changes         |

### Background Workers

| Worker              | Trigger                | Cost                                                          |
| ------------------- | ---------------------- | ------------------------------------------------------------- |
| **Context Updater** | Every push             | $0 -- deterministic reindex of changed files                  |
| **Reconciliation**  | Every 6 hours + manual | $0 -- syncs checkbox states, auto-closes completed milestones |

<br>

## Architecture

```
GitHub Webhook -> Cloudflare Tunnel -> FastAPI /api/webhooks/github
  -> verify signature -> upsert repo -> dispatch_event()
  -> Supervisor Agent (classifies event, picks agents)
  -> Specialist Agent(s) run tool-calling loop -> GitHub API + DB
```

### Directory Structure

```
src/
  agents/          -- Supervisor + 5 specialist agents with tool-calling loops
  api/             -- FastAPI routes (webhooks, health, dashboard, reconcile)
  core/            -- Config, database, GitHub auth, security, logging
  indexer/         -- Code indexer: parsers, downloader, code map generator, tree-sitter
  models/          -- SQLAlchemy ORM (Repository, Issue, Milestone, CodeIndex, etc.)
  tools/           -- Stateless tools grouped by domain (github/, ai/, db/)
  utils/           -- Shared utilities (checklist parsing)
  workers/         -- ARQ background tasks (context updater, reconciliation)
prompts/           -- Agent system prompts (one per agent + per pass)
static/            -- Dashboard frontend (HTML/JS/CSS)
alembic/           -- Database migrations
```

<br>

## Quick Start

### Prerequisites

- Docker & Docker Compose
- A [GitHub App](https://docs.github.com/en/apps/creating-github-apps) with webhook permissions
- An [OpenRouter](https://openrouter.ai/) API key
- A [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (or any webhook proxy)

### Setup

```bash
# Clone
git clone https://github.com/r-log/gita.git
cd gita

# Configure
cp .env.example .env
# Edit .env with your GitHub App ID, private key path, webhook secret,
# OpenRouter API key, and Cloudflare tunnel token

# Run
docker compose up --build -d

# Apply database migrations
docker compose exec app alembic upgrade head

# Follow logs
docker compose logs -f app
```

### Install on a Repo

1. Go to your GitHub App settings -> Install App
2. Select a repository
3. GITA starts indexing immediately -- check the Issues tab in a few seconds

<br>

## Configuration

All config is in `.env`. Key variables:

| Variable                      | Description                                          |
| ----------------------------- | ---------------------------------------------------- |
| `GITHUB_APP_ID`               | Your GitHub App ID                                   |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Path to the `.pem` file                              |
| `GITHUB_WEBHOOK_SECRET`       | Webhook secret for signature verification            |
| `OPENROUTER_API_KEY`          | OpenRouter API key                                   |
| `AI_DEFAULT_MODEL`            | Default LLM model (e.g. `anthropic/claude-sonnet-4`) |

### Per-Agent Model Overrides

Each agent can use a different model. Override via environment variables:

```bash
AI_MODEL_SUPERVISOR=anthropic/claude-haiku-4.5          # cheap classifier
AI_MODEL_ONBOARDING_PASS3=anthropic/claude-sonnet-4     # milestone reasoning
AI_MODEL_ONBOARDING_PASS4=anthropic/claude-haiku-4.5    # issue creation
AI_MODEL_RISK_DETECTIVE=anthropic/claude-opus-4.5        # security scanning
```

<br>

## Dashboard

GITA includes a built-in monitoring dashboard at `/dashboard`:

- **Repos** -- all installed repositories
- **Runs** -- onboarding and agent run history
- **Activity** -- event timeline with cost tracking
- **Alerts** -- failed runs, high-cost operations, stale repos
- **Costs** -- per-model token usage breakdown
- **Quick Actions** -- trigger reconciliation, re-run onboarding

<br>

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint & format
ruff check src/ tests/
ruff format src/ tests/

# Type check
mypy src/
```

<br>

## Built With

|             |                                                              |
| ----------- | ------------------------------------------------------------ |
| **Runtime** | Python 3.11+, FastAPI, ARQ workers                           |
| **Data**    | PostgreSQL, Redis, SQLAlchemy (async), Alembic               |
| **AI**      | OpenRouter (OpenAI-compatible), per-agent model selection    |
| **Parsing** | Python ast, tree-sitter-languages (Go/Java/Rust/C#/Ruby/PHP) |
| **Infra**   | Docker Compose, Cloudflare Tunnel                            |

<br>

## License

MIT
