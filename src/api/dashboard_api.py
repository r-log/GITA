"""
Dashboard API endpoints — serves data for the monitoring dashboard.
All endpoints are read-only queries against the existing DB models.
"""

import json
from datetime import datetime, timedelta

import structlog
from fastapi import APIRouter, Query, Request
from sqlalchemy import select, func, case, desc

from src.core.config import settings
from src.core.database import async_session
from src.models.repository import Repository
from src.models.onboarding_run import OnboardingRun
from src.models.agent_run import AgentRun
from src.models.analysis import Analysis
from src.models.event import EventModel
from src.models.outcome import OutcomeModel
from src.models.issue import IssueModel
from src.models.pull_request import PullRequestModel

log = structlog.get_logger()

router = APIRouter(prefix="/api/dashboard")


def _serialize_datetime(dt):
    return dt.isoformat() if dt else None


# ── Repos ──────────────────────────────────────────────────────────

@router.get("/repos")
async def list_repos():
    """List all tracked repositories."""
    async with async_session() as session:
        result = await session.execute(
            select(Repository).order_by(Repository.full_name)
        )
        repos = result.scalars().all()

    return [
        {
            "id": r.id,
            "full_name": r.full_name,
            "github_id": r.github_id,
            "installation_id": r.installation_id,
            "created_at": _serialize_datetime(r.created_at),
        }
        for r in repos
    ]


# ── Stats ──────────────────────────────────────────────────────────

@router.get("/stats")
async def get_stats(repo_id: int = Query(...)):
    """Aggregated stats for a repo."""
    async with async_session() as session:
        # Onboarding run counts
        onboarding_result = await session.execute(
            select(
                func.count(OnboardingRun.id).label("total"),
                func.max(OnboardingRun.completed_at).label("last_completed"),
            ).where(OnboardingRun.repo_id == repo_id)
        )
        onboarding_row = onboarding_result.one()

        # Last context update
        context_result = await session.execute(
            select(OnboardingRun.completed_at)
            .where(OnboardingRun.repo_id == repo_id, OnboardingRun.status == "context_update")
            .order_by(desc(OnboardingRun.completed_at))
            .limit(1)
        )
        last_context = context_result.scalar_one_or_none()

        # Agent run counts by name
        agent_counts_result = await session.execute(
            select(AgentRun.agent_name, func.count(AgentRun.id))
            .where(AgentRun.repo_id == repo_id)
            .group_by(AgentRun.agent_name)
        )
        agent_counts = {name: count for name, count in agent_counts_result.all()}

        # Agent run counts by status
        status_result = await session.execute(
            select(AgentRun.status, func.count(AgentRun.id))
            .where(AgentRun.repo_id == repo_id)
            .group_by(AgentRun.status)
        )
        status_counts = {status: count for status, count in status_result.all()}

        # Total agent runs
        total_agent_runs = sum(status_counts.values())

        # Issues from latest plan
        plan_result = await session.execute(
            select(OnboardingRun.suggested_plan)
            .where(
                OnboardingRun.repo_id == repo_id,
                OnboardingRun.status.in_(["success", "partial", "context_update"]),
            )
            .order_by(desc(OnboardingRun.completed_at))
            .limit(1)
        )
        plan = plan_result.scalar_one_or_none() or {}
        milestones = plan.get("milestones", [])

        issue_stats = {"total": 0, "done": 0, "in_progress": 0, "not_started": 0}
        for m in milestones:
            for t in m.get("tasks", []):
                issue_stats["total"] += 1
                status = t.get("status", "not-started")
                if status in ("done", "complete"):
                    issue_stats["done"] += 1
                elif status == "in-progress":
                    issue_stats["in_progress"] += 1
                else:
                    issue_stats["not_started"] += 1

    return {
        "total_onboarding_runs": onboarding_row.total,
        "total_agent_runs": total_agent_runs,
        "last_onboarding": _serialize_datetime(onboarding_row.last_completed),
        "last_context_update": _serialize_datetime(last_context),
        "agent_run_counts": agent_counts,
        "status_counts": status_counts,
        "issues_in_plan": issue_stats,
        "milestones_count": len(milestones),
    }


# ── Onboarding Runs ───────────────────────────────────────────────

@router.get("/runs")
async def list_runs(
    repo_id: int = Query(...),
    status: str = Query(None),
    limit: int = Query(50),
):
    """List onboarding runs for a repo."""
    async with async_session() as session:
        stmt = (
            select(OnboardingRun)
            .where(OnboardingRun.repo_id == repo_id)
            .order_by(desc(OnboardingRun.completed_at))
            .limit(limit)
        )
        if status:
            stmt = stmt.where(OnboardingRun.status == status)

        result = await session.execute(stmt)
        runs = result.scalars().all()

    return [
        {
            "id": r.id,
            "status": r.status,
            "issues_created": r.issues_created,
            "issues_updated": r.issues_updated,
            "milestones_created": r.milestones_created,
            "confidence": r.confidence,
            "started_at": _serialize_datetime(r.started_at),
            "completed_at": _serialize_datetime(r.completed_at),
            "actions_count": len(r.actions_taken) if isinstance(r.actions_taken, list) else 0,
        }
        for r in runs
    ]


@router.get("/run/{run_id}")
async def get_run(run_id: int):
    """Full detail for a single onboarding run."""
    async with async_session() as session:
        result = await session.execute(
            select(OnboardingRun).where(OnboardingRun.id == run_id)
        )
        run = result.scalar_one_or_none()

    if not run:
        return {"error": "Run not found"}

    return {
        "id": run.id,
        "repo_id": run.repo_id,
        "status": run.status,
        "repo_snapshot": run.repo_snapshot,
        "suggested_plan": run.suggested_plan,
        "existing_state": run.existing_state,
        "actions_taken": run.actions_taken,
        "issues_created": run.issues_created,
        "issues_updated": run.issues_updated,
        "milestones_created": run.milestones_created,
        "milestones_updated": run.milestones_updated,
        "confidence": run.confidence,
        "started_at": _serialize_datetime(run.started_at),
        "completed_at": _serialize_datetime(run.completed_at),
    }


# ── Agent Runs ─────────────────────────────────────────────────────

@router.get("/agents")
async def list_agent_runs(
    repo_id: int = Query(...),
    agent_name: str = Query(None),
    status: str = Query(None),
    limit: int = Query(50),
):
    """List agent runs with filtering."""
    async with async_session() as session:
        stmt = (
            select(AgentRun)
            .where(AgentRun.repo_id == repo_id)
            .order_by(desc(AgentRun.started_at))
            .limit(limit)
        )
        if agent_name:
            stmt = stmt.where(AgentRun.agent_name == agent_name)
        if status:
            stmt = stmt.where(AgentRun.status == status)

        result = await session.execute(stmt)
        runs = result.scalars().all()

    return [
        {
            "id": r.id,
            "agent_name": r.agent_name,
            "event_type": r.event_type,
            "status": r.status,
            "confidence": r.confidence,
            "duration_ms": r.duration_ms,
            "error_message": r.error_message,
            "tools_count": len(r.tools_called) if isinstance(r.tools_called, list) else 0,
            "started_at": _serialize_datetime(r.started_at),
            "completed_at": _serialize_datetime(r.completed_at),
        }
        for r in runs
    ]


@router.get("/agent/{run_id}")
async def get_agent_run(run_id: int):
    """Full detail for a single agent run."""
    async with async_session() as session:
        result = await session.execute(
            select(AgentRun).where(AgentRun.id == run_id)
        )
        run = result.scalar_one_or_none()

    if not run:
        return {"error": "Agent run not found"}

    return {
        "id": run.id,
        "agent_name": run.agent_name,
        "event_type": run.event_type,
        "status": run.status,
        "confidence": run.confidence,
        "duration_ms": run.duration_ms,
        "error_message": run.error_message,
        "context": run.context,
        "tools_called": run.tools_called,
        "result": run.result,
        "started_at": _serialize_datetime(run.started_at),
        "completed_at": _serialize_datetime(run.completed_at),
    }


# ── Analyses ───────────────────────────────────────────────────────

@router.get("/analyses")
async def list_analyses(
    repo_id: int = Query(...),
    analysis_type: str = Query(None),
    limit: int = Query(20),
):
    """List analysis records."""
    async with async_session() as session:
        stmt = (
            select(Analysis)
            .where(Analysis.repo_id == repo_id)
            .order_by(desc(Analysis.created_at))
            .limit(limit)
        )
        if analysis_type:
            stmt = stmt.where(Analysis.analysis_type == analysis_type)

        result = await session.execute(stmt)
        analyses = result.scalars().all()

    return [
        {
            "id": a.id,
            "target_type": a.target_type,
            "target_number": a.target_number,
            "analysis_type": a.analysis_type,
            "score": a.score,
            "risk_level": a.risk_level,
            "result": a.result,
            "created_at": _serialize_datetime(a.created_at),
        }
        for a in analyses
    ]


# ── Activity Timeline ──────────────────────────────────────────────

@router.get("/activity")
async def get_activity(
    repo_id: int = Query(...),
    days: int = Query(30),
):
    """Agent runs grouped by day for timeline chart."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with async_session() as session:
        result = await session.execute(
            select(
                func.date(AgentRun.started_at).label("date"),
                func.count(AgentRun.id).label("total"),
                func.count(case((AgentRun.status == "success", 1))).label("success"),
                func.count(case((AgentRun.status == "failed", 1))).label("failed"),
            )
            .where(AgentRun.repo_id == repo_id, AgentRun.started_at >= cutoff)
            .group_by(func.date(AgentRun.started_at))
            .order_by(func.date(AgentRun.started_at))
        )
        rows = result.all()

    return [
        {
            "date": str(r.date),
            "total": r.total,
            "success": r.success,
            "failed": r.failed,
        }
        for r in rows
    ]


# ── Issues from Plan ───────────────────────────────────────────────

@router.get("/issues")
async def get_issues_from_plan(repo_id: int = Query(...)):
    """Get milestone/issue structure from the latest stored plan."""
    async with async_session() as session:
        result = await session.execute(
            select(OnboardingRun.suggested_plan)
            .where(
                OnboardingRun.repo_id == repo_id,
                OnboardingRun.status.in_(["success", "partial", "context_update"]),
            )
            .order_by(desc(OnboardingRun.completed_at))
            .limit(1)
        )
        plan = result.scalar_one_or_none()

    if not plan:
        return {"milestones": []}

    milestones = plan.get("milestones", [])
    formatted = []
    for m in milestones:
        tasks = m.get("tasks", [])
        done = sum(1 for t in tasks if t.get("status") in ("done", "complete"))
        formatted.append({
            "title": m.get("title", ""),
            "description": m.get("description", ""),
            "confidence": m.get("confidence", 0),
            "total_tasks": len(tasks),
            "done_tasks": done,
            "progress_pct": round(done / len(tasks) * 100) if tasks else 0,
            "tasks": [
                {
                    "title": t.get("title", ""),
                    "status": t.get("status", "not-started"),
                    "effort": t.get("effort", ""),
                    "labels": t.get("labels", []),
                    "files": t.get("files", []),
                }
                for t in tasks
            ],
        })

    return {"milestones": formatted}


# ── Alerts ─────────────────────────────────────────────────────────

@router.get("/alerts")
async def get_alerts(repo_id: int = Query(...)):
    """Aggregate alert-worthy items: security findings, failed agents, stale issues."""
    critical = []
    warnings = []
    info_count = 0

    async with async_session() as session:
        # 1. Security findings from analyses
        analyses_result = await session.execute(
            select(Analysis)
            .where(
                Analysis.repo_id == repo_id,
                Analysis.risk_level.in_(["critical", "warning"]),
            )
            .order_by(desc(Analysis.created_at))
            .limit(20)
        )
        for a in analyses_result.scalars().all():
            findings = a.result or {}
            for f in findings.get("findings", {}).get("critical", []):
                critical.append({
                    "type": "security",
                    "message": f.get("description", f.get("type", "Unknown")),
                    "source": f"analysis #{a.id}",
                    "recommendation": f.get("recommendation", ""),
                    "created_at": _serialize_datetime(a.created_at),
                })
            for f in findings.get("findings", {}).get("warning", []):
                warnings.append({
                    "type": "security",
                    "message": f.get("description", f.get("type", "Unknown")),
                    "source": f"analysis #{a.id}",
                    "recommendation": f.get("recommendation", ""),
                    "created_at": _serialize_datetime(a.created_at),
                })
            info_count += len(findings.get("findings", {}).get("info", []))

        # 2. Failed agent runs in last 7 days
        cutoff = datetime.utcnow() - timedelta(days=7)
        failed_result = await session.execute(
            select(AgentRun)
            .where(
                AgentRun.repo_id == repo_id,
                AgentRun.status == "failed",
                AgentRun.started_at >= cutoff,
            )
            .order_by(desc(AgentRun.started_at))
            .limit(10)
        )
        for r in failed_result.scalars().all():
            error = r.error_message or "Unknown error"
            warnings.append({
                "type": "agent_failure",
                "message": f"{r.agent_name} failed: {error[:200]}",
                "source": f"agent_run #{r.id}",
                "created_at": _serialize_datetime(r.started_at),
            })

    return {
        "critical": critical,
        "warnings": warnings,
        "info_count": info_count,
        "total": len(critical) + len(warnings),
    }


# ── Costs ──────────────────────────────────────────────────────────

@router.get("/costs")
async def get_costs(
    repo_id: int = Query(...),
    days: int = Query(30),
):
    """Token usage and estimated costs from agent runs."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with async_session() as session:
        # Get agent runs with result data (usage is stored in result JSONB)
        result = await session.execute(
            select(AgentRun.agent_name, AgentRun.result, AgentRun.started_at)
            .where(AgentRun.repo_id == repo_id, AgentRun.started_at >= cutoff)
            .order_by(desc(AgentRun.started_at))
        )
        rows = result.all()

    pricing = settings.model_pricing
    fallback_input = settings.ai_cost_per_million_input
    fallback_output = settings.ai_cost_per_million_output

    def _calc_cost_for_usage(usage: dict) -> float:
        """Calculate cost using per-model pricing from by_model breakdown."""
        by_model = usage.get("by_model", {})
        if by_model:
            cost = 0.0
            for model_id, model_usage in by_model.items():
                ip, op = pricing.get(model_id, (fallback_input, fallback_output))
                cost += (model_usage.get("prompt_tokens", 0) * ip + model_usage.get("completion_tokens", 0) * op) / 1_000_000
            return cost
        # Fallback: no per-model data, use default rates
        return (usage.get("prompt_tokens", 0) * fallback_input + usage.get("completion_tokens", 0) * fallback_output) / 1_000_000

    total_prompt = 0
    total_completion = 0
    total_calls = 0
    total_cost = 0.0
    by_agent = {}
    daily = {}

    for agent_name, res, started_at in rows:
        usage = (res or {}).get("usage") or (res or {}).get("data", {}).get("usage") or {}
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        lc = usage.get("llm_calls", 0)
        run_cost = _calc_cost_for_usage(usage)

        total_prompt += pt
        total_completion += ct
        total_calls += lc
        total_cost += run_cost

        if agent_name not in by_agent:
            by_agent[agent_name] = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "runs": 0, "cost_usd": 0.0}
        by_agent[agent_name]["prompt_tokens"] += pt
        by_agent[agent_name]["completion_tokens"] += ct
        by_agent[agent_name]["llm_calls"] += lc
        by_agent[agent_name]["runs"] += 1
        by_agent[agent_name]["cost_usd"] += run_cost

        day = str(started_at.date()) if started_at else "unknown"
        if day not in daily:
            daily[day] = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
        daily[day]["prompt_tokens"] += pt
        daily[day]["completion_tokens"] += ct
        daily[day]["cost_usd"] += run_cost

    # Round costs
    for agent in by_agent.values():
        agent["cost_usd"] = round(agent["cost_usd"], 4)
    for day in daily.values():
        day["cost_usd"] = round(day["cost_usd"], 4)

    return {
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_llm_calls": total_calls,
        "total_cost_usd": round(total_cost, 4),
        "by_agent": by_agent,
        "daily": dict(sorted(daily.items())),
        "period_days": days,
    }


# ── GITA Timeline (event-centric view) ────────────────────────


def _summarize_tool_calls(tools_called: list) -> list[dict]:
    """Condense tool call log into readable actions."""
    if not tools_called:
        return []
    actions = []
    for tc in tools_called:
        tool = tc.get("tool", "unknown")
        args = tc.get("args", {})
        success = tc.get("result", {}).get("success", True)

        # Build a human-readable summary
        if tool == "post_comment":
            target = args.get("issue_number", "?")
            actions.append({"action": f"Commented on #{target}", "tool": tool, "success": success})
        elif tool == "upsert_progress_comment":
            target = args.get("issue_number", "?")
            actions.append({"action": f"Updated progress on #{target}", "tool": tool, "success": success})
        elif tool == "update_issue":
            target = args.get("issue_number", "?")
            state = args.get("state", "")
            if state == "closed":
                actions.append({"action": f"Closed #{target}", "tool": tool, "success": success})
            elif state == "open":
                actions.append({"action": f"Reopened #{target}", "tool": tool, "success": success})
            else:
                actions.append({"action": f"Updated #{target}", "tool": tool, "success": success})
        elif tool == "create_check_run":
            conclusion = args.get("conclusion", "?")
            actions.append({"action": f"Check run: {conclusion}", "tool": tool, "success": success})
        elif tool == "save_analysis":
            atype = args.get("analysis_type", "?")
            target = args.get("target_number", "?")
            actions.append({"action": f"Saved {atype} analysis for #{target}", "tool": tool, "success": success})
        elif tool == "save_evaluation":
            actions.append({"action": "Saved S.M.A.R.T. evaluation", "tool": tool, "success": success})
        elif tool == "tag_user":
            actions.append({"action": "Tagged users", "tool": tool, "success": success})
        elif tool == "create_issue":
            title = args.get("title", "?")
            actions.append({"action": f"Created issue: {title}", "tool": tool, "success": success})
        elif tool in ("get_issue", "get_pr", "get_issue_full", "search_comments", "search_events", "get_previous_evaluation", "predict_completion"):
            # Read-only tools — skip in summary
            continue
        else:
            actions.append({"action": f"{tool}({', '.join(f'{k}={v}' for k, v in list(args.items())[:2])})", "tool": tool, "success": success})
    return actions


def _describe_event(event_type: str, action: str, payload: dict) -> str:
    """Generate a one-line human-readable description of what triggered GITA."""
    if event_type == "push":
        commits = payload.get("commits", [])
        pusher = payload.get("pusher", {}).get("name", "someone")
        files = set()
        for c in commits:
            files.update(c.get("added", []))
            files.update(c.get("modified", []))
        msg = commits[0].get("message", "").split("\n")[0] if commits else ""
        return f"{pusher} pushed {len(commits)} commit(s) touching {len(files)} files: \"{msg}\""

    elif event_type == "issues":
        issue = payload.get("issue", {})
        who = issue.get("user", {}).get("login", "someone")
        title = issue.get("title", "")
        num = issue.get("number", "?")
        return f"{who} {action} issue #{num}: \"{title}\""

    elif event_type == "pull_request":
        pr = payload.get("pull_request", {})
        who = pr.get("user", {}).get("login", "someone")
        title = pr.get("title", "")
        num = pr.get("number", "?")
        return f"{who} {action} PR #{num}: \"{title}\""

    elif event_type == "issue_comment":
        comment = payload.get("comment", {})
        issue = payload.get("issue", {})
        who = comment.get("user", {}).get("login", "someone")
        num = issue.get("number", "?")
        body_preview = (comment.get("body", "") or "")[:100]
        return f"{who} commented on #{num}: \"{body_preview}\""

    elif event_type in ("installation", "installation_repositories"):
        return f"App {action} on repository"

    else:
        return f"{event_type}.{action}"


@router.get("/timeline")
async def get_timeline(
    repo_id: int = Query(...),
    limit: int = Query(30),
):
    """
    Event-centric timeline: each entry is a webhook event with the full chain.

    Returns:
    - What triggered GITA (the event)
    - What GITA decided to do (supervisor routing)
    - What each agent saw and did (tool calls, actions taken)
    - The outcome (success/failed, what changed)
    """
    async with async_session() as session:
        # Get recent events (exclude noise events that GITA doesn't act on)
        noise_events = {"check_suite", "check_run", "workflow_run", "workflow_job", "status", "deployment", "deployment_status", "create", "delete", "fork", "watch", "star"}
        events_result = await session.execute(
            select(EventModel)
            .where(
                EventModel.repo_id == repo_id,
                EventModel.event_type.notin_(noise_events),
            )
            .order_by(desc(EventModel.received_at))
            .limit(limit)
        )
        events = events_result.scalars().all()

        if not events:
            return {"timeline": []}

        # Get all agent runs for this repo in the same time window
        oldest_event = min(e.received_at for e in events)
        runs_result = await session.execute(
            select(AgentRun)
            .where(
                AgentRun.repo_id == repo_id,
                AgentRun.started_at >= oldest_event,
            )
            .order_by(AgentRun.started_at)
        )
        all_runs = runs_result.scalars().all()

    # Match agent runs to events by event_type and time proximity
    # Events and runs are correlated by: event_type matches, started within 5s of event
    timeline = []

    for event in events:
        event_key = f"{event.event_type}.{event.action}" if event.action else event.event_type

        # Find agent runs triggered by this event (within 5 seconds)
        matched_runs = []
        for run in all_runs:
            if run.event_type == event_key:
                time_diff = abs((run.started_at - event.received_at).total_seconds()) if run.started_at and event.received_at else 999
                if time_diff < 5:
                    matched_runs.append(run)

        # Build the chain
        agents_chain = []
        for run in matched_runs:
            tool_actions = _summarize_tool_calls(run.tools_called or [])
            tools_count = len(run.tools_called) if isinstance(run.tools_called, list) else 0

            # Extract the final text / comment from result
            result_data = run.result or {}
            final_text = result_data.get("final_response", "")
            comment_body = result_data.get("comment_body", "")

            agents_chain.append({
                "agent": run.agent_name,
                "status": run.status,
                "duration_ms": run.duration_ms,
                "tools_used": tools_count,
                "actions": tool_actions,
                "confidence": run.confidence,
                "error": run.error_message,
                "summary": final_text[:300] if final_text else (comment_body[:300] if comment_body else None),
            })

        # Build the timeline entry
        entry = {
            "id": event.id,
            "timestamp": _serialize_datetime(event.received_at),
            "event_type": event.event_type,
            "action": event.action,
            "event_key": event_key,
            "sender": event.sender_login,
            "target_type": event.target_type,
            "target_number": event.target_number,
            "description": _describe_event(event.event_type, event.action or "", event.payload),
            "agents_dispatched": len(matched_runs),
            "agents": agents_chain,
            "overall_status": (
                "success" if all(r["status"] == "success" for r in agents_chain)
                else "failed" if any(r["status"] == "failed" for r in agents_chain)
                else "partial" if agents_chain
                else "no_action"
            ),
            "total_duration_ms": sum(r["duration_ms"] or 0 for r in agents_chain),
        }
        timeline.append(entry)

    return {"timeline": timeline}


# ── Outcomes ───────────────────────────────────────────────────────


_ACTION_VERBS = {
    "smart_eval": "flagged quality issues on",
    "closure_validation": "validated the closure of",
    "checklist_correction": "corrected the checklist in",
    "risk_warning": "warned about risks in",
    "stale_nudge": "nudged the stale",
    "deadline_prediction": "predicted the deadline for",
}


def _build_story(row: dict, title: str | None) -> str:
    """Turn a raw outcome row into a one-sentence human-readable story."""
    target_label = f"{row['target_type']} #{row['target_number']}"
    if title:
        target_label += f" \"{title[:60]}\""
    verb = _ACTION_VERBS.get(row["outcome_type"], "intervened on")
    verdict = row.get("notes") or {
        "success": "intervention landed",
        "partial": "partially landed",
        "failed": "no measurable effect",
        "pending": "awaiting result",
        "stale": "no result in 7 days",
    }.get(row["status"], row["status"])
    return f"{row['agent_name']} {verb} {target_label} — {verdict}"


@router.get("/outcomes")
async def get_outcomes(
    repo_id: int = Query(...),
    days: int = Query(30),
):
    """
    Outcomes rollup — tells the human whether GITA is pulling its weight.

    Returns a narrative, not a table:
    - headline: one-sentence verdict for the period
    - trend: weekly success/partial/failed buckets for a line chart
    - by_agent: who's pulling their weight
    - wins: recent stories where GITA helped
    - struggles: recent stories where GITA missed
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    async with async_session() as session:
        # Pull all outcomes for the period with agent name
        rows_stmt = (
            select(OutcomeModel, AgentRun.agent_name)
            .join(AgentRun, OutcomeModel.agent_run_id == AgentRun.id)
            .where(
                OutcomeModel.repo_id == repo_id,
                OutcomeModel.created_at >= cutoff,
            )
            .order_by(desc(OutcomeModel.created_at))
        )
        joined = (await session.execute(rows_stmt)).all()

        # Bulk-load titles for target enrichment
        issue_numbers = {o.target_number for (o, _) in joined if o.target_type == "issue" and o.target_number}
        pr_numbers = {o.target_number for (o, _) in joined if o.target_type == "pr" and o.target_number}

        issue_titles: dict[int, str] = {}
        if issue_numbers:
            issue_rows = (await session.execute(
                select(IssueModel.github_number, IssueModel.title).where(
                    IssueModel.repo_id == repo_id,
                    IssueModel.github_number.in_(issue_numbers),
                )
            )).all()
            issue_titles = {num: title for (num, title) in issue_rows}

        pr_titles: dict[int, str] = {}
        if pr_numbers:
            pr_rows = (await session.execute(
                select(PullRequestModel.github_number, PullRequestModel.title).where(
                    PullRequestModel.repo_id == repo_id,
                    PullRequestModel.github_number.in_(pr_numbers),
                )
            )).all()
            pr_titles = {num: title for (num, title) in pr_rows}

    # Shape rows + enrich with titles + stories
    shaped = []
    for o, agent_name in joined:
        title = None
        if o.target_type == "issue":
            title = issue_titles.get(o.target_number)
        elif o.target_type == "pr":
            title = pr_titles.get(o.target_number)

        row = {
            "id": o.id,
            "agent_name": agent_name,
            "outcome_type": o.outcome_type,
            "target_type": o.target_type,
            "target_number": o.target_number,
            "target_title": title,
            "status": o.status,
            "notes": o.notes,
            "scheduled_for": _serialize_datetime(o.scheduled_for),
            "checked_at": _serialize_datetime(o.checked_at),
            "created_at": _serialize_datetime(o.created_at),
        }
        row["story"] = _build_story(row, title)
        shaped.append(row)

    # Headline counts
    counts = {"success": 0, "partial": 0, "failed": 0, "pending": 0, "stale": 0}
    for r in shaped:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    total = len(shaped)
    checked = counts["success"] + counts["partial"] + counts["failed"]
    success_rate = round(counts["success"] / checked * 100, 1) if checked else None

    # First-result ETA: earliest scheduled_for among pending rows
    pending_rows = [r for r in shaped if r["status"] == "pending" and r["scheduled_for"]]
    first_eta = min((r["scheduled_for"] for r in pending_rows), default=None)

    # Human headline
    if total == 0:
        headline_text = f"No interventions in the last {days} days yet."
    elif checked == 0:
        headline_text = (
            f"Still gathering signal — {total} interventions scheduled, "
            f"first results in ~24h."
        )
    else:
        parts = []
        if counts["success"]:
            parts.append(f"{counts['success']} worked")
        if counts["partial"]:
            parts.append(f"{counts['partial']} partially")
        if counts["failed"]:
            parts.append(f"{counts['failed']} didn't land")
        if counts["pending"]:
            parts.append(f"{counts['pending']} still pending")
        headline_text = f"{total} interventions in {days} days — " + ", ".join(parts) + "."

    # Weekly trend — last 8 weeks, bucketed by ISO week start (Monday)
    now = datetime.utcnow()
    trend_weeks = 8
    trend_cutoff = now - timedelta(weeks=trend_weeks)
    buckets: dict[str, dict[str, int]] = {}
    for i in range(trend_weeks):
        wk_start = now - timedelta(days=now.weekday(), weeks=i)
        wk_start = wk_start.replace(hour=0, minute=0, second=0, microsecond=0)
        key = wk_start.date().isoformat()
        buckets[key] = {"success": 0, "partial": 0, "failed": 0, "total": 0}

    for r in shaped:
        if not r["created_at"]:
            continue
        created = datetime.fromisoformat(r["created_at"].replace("Z", ""))
        if created < trend_cutoff:
            continue
        wk_start = created - timedelta(days=created.weekday())
        wk_start = wk_start.replace(hour=0, minute=0, second=0, microsecond=0)
        key = wk_start.date().isoformat()
        if key not in buckets:
            continue
        buckets[key]["total"] += 1
        if r["status"] in ("success", "partial", "failed"):
            buckets[key][r["status"]] += 1

    trend = [
        {"week_start": k, **v}
        for k, v in sorted(buckets.items())
    ]

    # By-agent rollup
    by_agent: dict[str, dict] = {}
    for r in shaped:
        a = r["agent_name"]
        if a not in by_agent:
            by_agent[a] = {"success": 0, "partial": 0, "failed": 0, "pending": 0, "stale": 0, "total": 0}
        by_agent[a][r["status"]] = by_agent[a].get(r["status"], 0) + 1
        by_agent[a]["total"] += 1

    for stats in by_agent.values():
        checked_ = stats["success"] + stats["partial"] + stats["failed"]
        stats["checked"] = checked_
        stats["success_rate"] = round(stats["success"] / checked_ * 100, 1) if checked_ else None

    # Wins + struggles lists (top 10 each, most recent first)
    wins = [r for r in shaped if r["status"] == "success"][:10]
    struggles = [r for r in shaped if r["status"] == "failed"][:10]

    return {
        "headline": {
            "period_days": days,
            "text": headline_text,
            "total": total,
            "checked": checked,
            "succeeded": counts["success"],
            "partial": counts["partial"],
            "failed": counts["failed"],
            "pending": counts["pending"],
            "stale": counts["stale"],
            "success_rate": success_rate,
            "first_result_eta": first_eta,
        },
        "trend": trend,
        "by_agent": by_agent,
        "wins": wins,
        "struggles": struggles,
    }


# ── Quick Actions ──────────────────────────────────────────────────

@router.post("/trigger")
async def trigger_action(request: Request):
    """Trigger an action: reconcile or full rescan."""
    body = {}
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return {"status": "error", "message": "Invalid JSON"}

    repo_id = body.get("repo_id")
    action = body.get("action", "reconcile")

    if not repo_id:
        return {"status": "error", "message": "repo_id required"}

    # Look up repo
    async with async_session() as session:
        result = await session.execute(
            select(Repository).where(Repository.id == repo_id)
        )
        repo = result.scalar_one_or_none()

    if not repo:
        return {"status": "error", "message": f"Repository not found: {repo_id}"}

    if action == "reconcile":
        from src.workers.reconciliation import reconcile_repo
        try:
            result = await reconcile_repo(repo.id, repo.full_name, repo.installation_id)
            return {"status": "ok", "action": "reconcile", "result": result}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    elif action == "rescan":
        from src.agents.base import AgentContext
        from src.agents.supervisor import SupervisorAgent

        context = AgentContext(
            event_type="installation_repositories.added",
            event_payload={"action": "added", "repositories_added": [
                {"id": repo.github_id, "full_name": repo.full_name}
            ]},
            repo_full_name=repo.full_name,
            installation_id=repo.installation_id,
            repo_id=repo.id,
        )
        try:
            supervisor = SupervisorAgent()
            result = await supervisor.handle(context)
            return {"status": "ok", "action": "rescan", "agent_status": result.status}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return {"status": "error", "message": f"Unknown action: {action}"}
