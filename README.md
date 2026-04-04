<div align="center">

# GITA

### **G**itHub **I**ntelligent **T**racking **A**ssistant

Install it on a repo. It reads your code, creates a project plan, and keeps it updated. No commands. No dashboards. It just works.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)](https://postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)](https://redis.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

</div>

---

## What happens when you install GITA

1. **Indexes your codebase** -- parses every file (Python, Go, Java, Rust, C#, Ruby, PHP, JS/TS) without using a single LLM token
2. **Creates milestones & issues** -- based on what's built, what's missing, and what needs work
3. **Keeps everything in sync** -- every push re-indexes changed files, closes completed issues, flags stale ones

Then it sticks around:

- **Reviews your PRs** -- diff quality, test coverage, linked issues
- **Evaluates new issues** -- S.M.A.R.T. scoring, milestone alignment
- **Tracks progress** -- velocity, blockers, deadline predictions
- **Catches risks** -- leaked secrets, vulnerabilities, breaking changes

---

## How it works

A **Supervisor** receives GitHub webhooks and dispatches **5 specialist agents** -- each with its own tools, model, and reasoning loop.

```
Webhook --> Supervisor --> Onboarding Agent     (index + plan)
                      --> Issue Analyst         (evaluate issues)
                      --> Progress Tracker      (velocity + blockers)
                      --> PR Reviewer           (diff + tests)
                      --> Risk Detective        (security + secrets)
```

The secret sauce: a **deterministic code indexer** parses your entire repo into a ~5KB code map (routes, models, services, gaps). Agents query this instead of reading files through the GitHub API. First run costs ~$0.14 in LLM calls. Updates cost ~$0.08.

---

## Quick start

```bash
git clone https://github.com/r-log/gita.git && cd gita
cp .env.example .env        # add your GitHub App + OpenRouter keys
docker compose up --build -d
docker compose exec app alembic upgrade head
```

Install the GitHub App on a repo. Issues appear in seconds.

---

## Tech stack

Python 3.11+ / FastAPI / PostgreSQL / Redis / ARQ workers / SQLAlchemy (async) / Alembic / OpenRouter / tree-sitter / Docker Compose / Cloudflare Tunnel

---

MIT
