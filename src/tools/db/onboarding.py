"""
DB tools for onboarding persistence.
"""

import structlog
from datetime import datetime

from src.core.database import async_session
from src.models.onboarding_run import OnboardingRun
from src.tools.base import ToolResult

log = structlog.get_logger()


async def _save_onboarding_run(
    repo_id: int,
    status: str,
    repo_snapshot: dict,
    suggested_plan: dict,
    existing_state: dict,
    actions_taken: list,
    milestones_created: int = 0,
    milestones_updated: int = 0,
    issues_created: int = 0,
    issues_updated: int = 0,
    confidence: float = 0.0,
) -> ToolResult:
    try:
        async with async_session() as session:
            run = OnboardingRun(
                repo_id=repo_id,
                status=status,
                repo_snapshot=repo_snapshot,
                suggested_plan=suggested_plan,
                existing_state=existing_state,
                actions_taken=actions_taken,
                milestones_created=milestones_created,
                milestones_updated=milestones_updated,
                issues_created=issues_created,
                issues_updated=issues_updated,
                confidence=confidence,
                completed_at=datetime.utcnow(),
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return ToolResult(success=True, data={"onboarding_run_id": run.id})
    except Exception as e:
        log.warning("onboarding_db_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))
