<div align="center">

# 🤖 GITA

### **G**itHub **I**ntelligent **T**racking **A**ssistant

*An AI-powered project assistant that lives inside your GitHub repos.*

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)](https://postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)](https://redis.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

---

**GITA monitors your repos in real-time** — analyzing issues, reviewing PRs, tracking milestones, and catching risks before they become problems. It thinks, decides, and acts — all through GitHub comments and check runs.

</div>

<br>

## ✨ What It Does

> Install GITA on a repo and it starts working immediately. No commands to run. No dashboards to check.

- 🔍 **Scans new repos** — understands your project structure, creates milestones and issues automatically
- 📋 **Evaluates issues** — scores them against S.M.A.R.T. criteria, suggests what's missing
- 📊 **Tracks progress** — calculates velocity, predicts deadlines, flags stale issues and blockers
- 🔀 **Reviews PRs** — analyzes code quality, checks test coverage, verifies linked issues
- 🛡️ **Catches risks** — scans for leaked secrets, security vulnerabilities, and breaking changes

<br>

## 🧠 How It Works

GITA is a **multi-agent system**. Instead of one monolithic bot, it has a team of specialist agents — each with its own expertise, tools, and reasoning loop.

The **Supervisor** receives every event and decides which agents to dispatch — often running them **in parallel**. Each agent picks from its own scoped toolset, reasons through the problem, and takes action.

Agents share tools, not logic.

<br>

## 🏗️ The Agents

| | Agent | Triggers | What It Does |
|---|-------|----------|-------------|
| 🔍 | **Onboarding** | App installed | Scans the codebase, infers milestones, reconciles with existing issues |
| 📋 | **Issue Analyst** | Issue opened/edited | S.M.A.R.T. evaluation, milestone alignment check, constructive feedback |
| 📊 | **Progress Tracker** | Milestone events, pushes | Velocity trends, blocker detection, deadline prediction |
| 🔀 | **PR Reviewer** | PR opened/updated | Diff quality analysis, test coverage, linked issue verification |
| 🛡️ | **Risk Detective** | PR opened/updated, pushes | Secret scanning, vulnerability patterns, breaking change detection |

<br>

## 🔧 Built With

<table>
<tr>
<td><strong>Runtime</strong></td>
<td>Python 3.11+ · FastAPI · ARQ workers</td>
</tr>
<tr>
<td><strong>Data</strong></td>
<td>PostgreSQL · Redis · SQLAlchemy (async) · Alembic</td>
</tr>
<tr>
<td><strong>AI</strong></td>
<td>OpenRouter (OpenAI-compatible) · any LLM per agent</td>
</tr>
<tr>
<td><strong>Infra</strong></td>
<td>Docker Compose · Cloudflare Tunnel</td>
</tr>
</table>

<br>

## 📄 License

MIT — do whatever you want with it.
