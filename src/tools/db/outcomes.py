"""
DB tools for outcome history — lets agents look up whether past interventions
on the same target succeeded or failed. This is the read-only surface; writes
are handled by the outcome scheduler and worker.
"""

from sqlalchemy import select, desc

import structlog

from src.core.database import async_session
from src.models.outcome import OutcomeModel
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


async def _get_outcome_history(
    repo_id: int,
    target_type: str | None = None,
    target_number: int | None = None,
    outcome_type: str | None = None,
    limit: int = 10,
) -> ToolResult:
    """
    Look up recent outcomes for a specific target or across the repo.

    Returns a list of outcome summaries ordered most-recent-first.
    Useful for agents to reason about "did our last intervention on this PR work?"
    """
    if not repo_id:
        return ToolResult(success=False, error="repo_id required")

    try:
        async with async_session() as session:
            stmt = (
                select(OutcomeModel)
                .where(OutcomeModel.repo_id == repo_id)
                .order_by(desc(OutcomeModel.created_at))
                .limit(min(limit, 50))
            )
            if target_type:
                stmt = stmt.where(OutcomeModel.target_type == target_type)
            if target_number is not None:
                stmt = stmt.where(OutcomeModel.target_number == target_number)
            if outcome_type:
                stmt = stmt.where(OutcomeModel.outcome_type == outcome_type)

            result = await session.execute(stmt)
            rows = result.scalars().all()

        data = [
            {
                "id": r.id,
                "outcome_type": r.outcome_type,
                "target_type": r.target_type,
                "target_number": r.target_number,
                "status": r.status,
                "predicted": r.predicted,
                "observed": r.observed,
                "notes": r.notes,
                "scheduled_for": r.scheduled_for.isoformat() if r.scheduled_for else None,
                "checked_at": r.checked_at.isoformat() if r.checked_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return ToolResult(success=True, data=data)
    except Exception as e:
        log.warning("get_outcome_history_failed", repo_id=repo_id, error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_get_outcome_history(repo_id: int) -> Tool:
    """Factory: make a get_outcome_history tool scoped to one repo."""
    return Tool(
        name="get_outcome_history",
        description=(
            "Look up past outcome records for a target (issue, PR, milestone) or across the repo. "
            "Use this to check whether past interventions on the same target succeeded or failed. "
            "Returns up to N recent outcomes with their predicted vs observed verdicts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target_type": {
                    "type": "string",
                    "enum": ["issue", "pr", "milestone"],
                    "description": "Filter by target type",
                },
                "target_number": {
                    "type": "integer",
                    "description": "Filter by specific issue/PR/milestone number",
                },
                "outcome_type": {
                    "type": "string",
                    "description": "Filter by outcome_type (smart_eval, risk_warning, etc.)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10, capped at 50)",
                },
            },
        },
        handler=lambda target_type=None, target_number=None, outcome_type=None, limit=10: _get_outcome_history(
            repo_id, target_type, target_number, outcome_type, limit
        ),
    )
