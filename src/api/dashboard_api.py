"""
Dashboard API endpoints — serves data for the monitoring dashboard.
All endpoints are read-only queries against the existing DB models.
"""

from datetime import datetime, timedelta

import structlog
from fastapi import APIRouter, Query
from sqlalchemy import select, func, case, desc

from src.core.database import async_session
from src.models.repository import Repository
from src.models.onboarding_run import OnboardingRun
from src.models.agent_run import AgentRun
from src.models.analysis import Analysis

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
