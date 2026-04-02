# GITA

**G**itHub **I**ntelligent **T**racking **A**ssistant

A GitHub App that acts as an AI-powered project assistant. It monitors repositories in real-time via webhooks, analyzes issues and pull requests, tracks milestone progress, detects risks, and posts actionable comments — all automatically.

## How It Works

GITA is built as a multi-agent system. A **Supervisor Agent** receives every webhook event and dispatches specialist agents to handle it:

| Agent | What It Does |
|-------|-------------|
| **Onboarding** | Scans new repos, infers project structure, creates milestones and issues |
| **Issue Analyst** | Evaluates issues against S.M.A.R.T. criteria, suggests improvements |
| **Progress Tracker** | Tracks velocity, predicts deadlines, flags blockers |
| **PR Reviewer** | Analyzes diffs for quality, checks test coverage, verifies linked issues |
| **Risk Detective** | Scans for secrets, security vulnerabilities, and breaking changes |

Each agent reasons autonomously using its own LLM-powered tool-calling loop with a scoped set of tools — agents share tools, not logic.

## Architecture

```
GitHub Webhooks → FastAPI → Supervisor Agent → Specialist Agent(s)
                                                    ↓
                                              Tool Layer
                                         (GitHub · AI · DB)
```

Agents run in parallel when appropriate. Results are merged, deduplicated, and posted as comments or check runs.

## License

MIT
