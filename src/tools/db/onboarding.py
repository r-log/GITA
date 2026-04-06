"""
DB tools for onboarding persistence.
"""

import structlog
from datetime import datetime

from sqlalchemy import select

from src.core.database import async_session
from src.models.onboarding_run import OnboardingRun

log = structlog.get_logger()
from src.models.file_mapping import FileMapping
from src.models.graph_node import GraphNode
from src.models.graph_edge import GraphEdge
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
        log.warning("onboarding_db_failed", error=str(e), exc_info=True)
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
            await session.flush()

            # Also create graph edges for the mapping
            file_node_result = await session.execute(
                select(GraphNode.id).where(
                    GraphNode.repo_id == repo_id,
                    GraphNode.file_path == file_path,
                    GraphNode.node_type == "file",
                )
            )
            file_node_id = file_node_result.scalar_one_or_none()

            if file_node_id:
                if milestone_id:
                    session.add(GraphEdge(
                        repo_id=repo_id,
                        source_node_id=file_node_id,
                        target_node_id=None,
                        edge_type="belongs_to_milestone",
                        target_entity_type="milestone",
                        target_entity_id=milestone_id,
                        confidence=confidence,
                    ))
                if issue_id:
                    session.add(GraphEdge(
                        repo_id=repo_id,
                        source_node_id=file_node_id,
                        target_node_id=None,
                        edge_type="belongs_to_issue",
                        target_entity_type="issue",
                        target_entity_id=issue_id,
                        confidence=confidence,
                    ))

            await session.commit()
            await session.refresh(mapping)
            return ToolResult(success=True, data={"file_mapping_id": mapping.id})
    except Exception as e:
        log.warning("onboarding_db_failed", error=str(e), exc_info=True)
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
