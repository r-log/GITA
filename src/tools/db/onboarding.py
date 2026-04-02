"""
DB tools for onboarding persistence.
"""

from datetime import datetime
from src.core.database import async_session
from src.models.onboarding_run import OnboardingRun
from src.models.file_mapping import FileMapping
from src.tools.base import Tool, ToolResult


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
        return ToolResult(success=False, error=str(e))


async def _save_file_mapping(
    repo_id: int,
    file_path: str,
    milestone_id: int | None = None,
    issue_id: int | None = None,
    confidence: float = 0.0,
) -> ToolResult:
    try:
        async with async_session() as session:
            mapping = FileMapping(
                repo_id=repo_id,
                file_path=file_path,
                milestone_id=milestone_id,
                issue_id=issue_id,
                confidence=confidence,
            )
            session.add(mapping)
            await session.commit()
            await session.refresh(mapping)
            return ToolResult(success=True, data={"file_mapping_id": mapping.id})
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_save_onboarding_run(repo_id: int) -> Tool:
    return Tool(
        name="save_onboarding_run",
        description="Save the onboarding run results to the database for future drift detection.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["success", "partial", "failed"]},
                "repo_snapshot": {"type": "object", "description": "Summary of the repo scan"},
                "suggested_plan": {"type": "object", "description": "AI-inferred milestones and tasks"},
                "existing_state": {"type": "object", "description": "What milestones/issues existed before"},
                "actions_taken": {"type": "array", "items": {"type": "object"}, "description": "List of actions taken (create/update/skip)"},
                "milestones_created": {"type": "integer"},
                "milestones_updated": {"type": "integer"},
                "issues_created": {"type": "integer"},
                "issues_updated": {"type": "integer"},
                "confidence": {"type": "number", "description": "Overall confidence 0.0-1.0"},
            },
            "required": ["status", "actions_taken"],
        },
        handler=lambda status, repo_snapshot=None, suggested_plan=None, existing_state=None,
                       actions_taken=None, milestones_created=0, milestones_updated=0,
                       issues_created=0, issues_updated=0, confidence=0.0: _save_onboarding_run(
            repo_id, status, repo_snapshot or {}, suggested_plan or {}, existing_state or {},
            actions_taken or [], milestones_created, milestones_updated, issues_created, issues_updated, confidence
        ),
    )


def make_save_file_mapping(repo_id: int) -> Tool:
    return Tool(
        name="save_file_mapping",
        description="Save a file-to-issue/milestone mapping for drift detection on future pushes.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File path relative to repo root"},
                "milestone_id": {"type": "integer", "description": "DB milestone ID (not GitHub number)"},
                "issue_id": {"type": "integer", "description": "DB issue ID (not GitHub number)"},
                "confidence": {"type": "number", "description": "Mapping confidence 0.0-1.0"},
            },
            "required": ["file_path"],
        },
        handler=lambda file_path, milestone_id=None, issue_id=None, confidence=0.0: _save_file_mapping(
            repo_id, file_path, milestone_id, issue_id, confidence
        ),
    )
