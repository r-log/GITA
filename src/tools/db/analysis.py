"""
DB tools for analysis persistence: S.M.A.R.T. evaluations, general analyses, progress snapshots.
"""

from datetime import datetime
from sqlalchemy import select, desc

from src.core.database import async_session
from src.models.smart_evaluation import SmartEvaluationModel
from src.models.issue import IssueModel
from src.models.analysis import Analysis
from src.tools.base import Tool, ToolResult

import structlog

log = structlog.get_logger()


async def _resolve_issue_db_id(repo_id: int, github_number: int) -> int | None:
    """Look up the DB issue ID from repo_id + GitHub issue number. Creates if missing."""
    if not repo_id or not github_number:
        return None
    try:
        async with async_session() as session:
            stmt = select(IssueModel).where(
                IssueModel.repo_id == repo_id,
                IssueModel.github_number == github_number,
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            if record:
                return record.id
            # Create a minimal issue record
            issue = IssueModel(repo_id=repo_id, github_number=github_number)
            session.add(issue)
            await session.commit()
            await session.refresh(issue)
            return issue.id
    except Exception as e:
        log.warning("resolve_issue_db_id_failed", repo_id=repo_id, github_number=github_number, error=str(e), exc_info=True)
        return None


async def _save_evaluation(
    repo_id: int,
    github_issue_number: int,
    is_milestone: bool,
    evaluation: dict,
) -> ToolResult:
    try:
        issue_db_id = await _resolve_issue_db_id(repo_id, github_issue_number)
        if not issue_db_id:
            return ToolResult(success=False, error="Could not resolve issue DB ID")

        async with async_session() as session:
            record = SmartEvaluationModel(
                issue_id=issue_db_id,
                is_milestone=is_milestone,
                overall_score=evaluation.get("overall_score"),
                specific_score=evaluation.get("specific", {}).get("score"),
                measurable_score=evaluation.get("measurable", {}).get("score"),
                achievable_score=evaluation.get("achievable", {}).get("score"),
                relevant_score=evaluation.get("relevant", {}).get("score"),
                time_bound_score=evaluation.get("time_bound", {}).get("score"),
                findings={k: v.get("findings", []) for k, v in evaluation.items() if isinstance(v, dict) and "findings" in v},
                suggestions={k: v.get("suggestions", []) for k, v in evaluation.items() if isinstance(v, dict) and "suggestions" in v},
                action_items=evaluation.get("action_items", []),
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return ToolResult(success=True, data={"evaluation_id": record.id})
    except Exception as e:
        log.warning("save_evaluation_failed", operation="save_evaluation", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _get_previous_evaluation(repo_id: int, github_issue_number: int) -> ToolResult:
    try:
        issue_db_id = await _resolve_issue_db_id(repo_id, github_issue_number)
        if not issue_db_id:
            return ToolResult(success=True, data=None)

        async with async_session() as session:
            stmt = (
                select(SmartEvaluationModel)
                .where(SmartEvaluationModel.issue_id == issue_db_id)
                .order_by(desc(SmartEvaluationModel.created_at))
                .limit(1)
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            if not record:
                return ToolResult(success=True, data=None)
            return ToolResult(success=True, data={
                "id": record.id,
                "overall_score": record.overall_score,
                "specific_score": record.specific_score,
                "measurable_score": record.measurable_score,
                "achievable_score": record.achievable_score,
                "relevant_score": record.relevant_score,
                "time_bound_score": record.time_bound_score,
                "created_at": record.created_at.isoformat() if record.created_at else None,
            })
    except Exception as e:
        log.warning("get_previous_evaluation_failed", operation="get_previous_evaluation", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _save_analysis(
    repo_id: int,
    target_type: str,
    target_number: int,
    analysis_type: str,
    result_data: dict,
    score: float | None = None,
    risk_level: str | None = None,
) -> ToolResult:
    try:
        async with async_session() as session:
            record = Analysis(
                repo_id=repo_id,
                target_type=target_type,
                target_number=target_number,
                analysis_type=analysis_type,
                result=result_data,
                score=score,
                risk_level=risk_level,
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return ToolResult(success=True, data={"analysis_id": record.id})
    except Exception as e:
        log.warning("save_analysis_failed", operation="save_analysis", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _get_analysis_history(
    repo_id: int,
    target_type: str,
    target_number: int,
    limit: int = 10,
) -> ToolResult:
    try:
        async with async_session() as session:
            stmt = (
                select(Analysis)
                .where(
                    Analysis.repo_id == repo_id,
                    Analysis.target_type == target_type,
                    Analysis.target_number == target_number,
                )
                .order_by(desc(Analysis.created_at))
                .limit(limit)
            )
            result = await session.execute(stmt)
            records = result.scalars().all()
            return ToolResult(success=True, data=[
                {
                    "id": r.id,
                    "analysis_type": r.analysis_type,
                    "score": r.score,
                    "risk_level": r.risk_level,
                    "result": r.result,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ])
    except Exception as e:
        log.warning("get_analysis_history_failed", operation="get_analysis_history", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_save_evaluation(repo_id: int) -> Tool:
    return Tool(
        name="save_evaluation",
        description="Save a S.M.A.R.T. evaluation result to the database.",
        parameters={
            "type": "object",
            "properties": {
                "github_issue_number": {"type": "integer", "description": "The GitHub issue number being evaluated"},
                "is_milestone": {"type": "boolean"},
                "evaluation": {"type": "object", "description": "Full evaluation result from evaluate_smart"},
            },
            "required": ["github_issue_number", "evaluation"],
        },
        handler=lambda github_issue_number, evaluation, is_milestone=False: _save_evaluation(
            repo_id, github_issue_number, is_milestone, evaluation
        ),
    )


def make_get_previous_evaluation(repo_id: int) -> Tool:
    return Tool(
        name="get_previous_evaluation",
        description="Fetch the most recent S.M.A.R.T. evaluation for an issue from the database.",
        parameters={
            "type": "object",
            "properties": {
                "github_issue_number": {"type": "integer", "description": "The GitHub issue number"},
            },
            "required": ["github_issue_number"],
        },
        handler=lambda github_issue_number: _get_previous_evaluation(repo_id, github_issue_number),
    )


def make_save_analysis(repo_id: int) -> Tool:
    return Tool(
        name="save_analysis",
        description="Save a general analysis result (issue, PR, or milestone) to the database.",
        parameters={
            "type": "object",
            "properties": {
                "target_type": {"type": "string", "enum": ["issue", "pr", "milestone"]},
                "target_number": {"type": "integer", "description": "GitHub issue/PR number"},
                "analysis_type": {"type": "string", "enum": ["smart", "risk", "quality", "progress"]},
                "result_data": {"type": "object", "description": "Analysis result data"},
                "score": {"type": "number"},
                "risk_level": {"type": "string", "enum": ["info", "warning", "critical"]},
            },
            "required": ["target_type", "target_number", "analysis_type", "result_data"],
        },
        handler=lambda target_type, target_number, analysis_type, result_data, score=None, risk_level=None: _save_analysis(
            repo_id, target_type, target_number, analysis_type, result_data, score, risk_level
        ),
    )


def make_get_analysis_history(repo_id: int) -> Tool:
    return Tool(
        name="get_analysis_history",
        description="Fetch past analysis records for a specific target from the database.",
        parameters={
            "type": "object",
            "properties": {
                "target_type": {"type": "string", "enum": ["issue", "pr", "milestone"]},
                "target_number": {"type": "integer"},
                "limit": {"type": "integer", "description": "Max records to return (default 10)"},
            },
            "required": ["target_type", "target_number"],
        },
        handler=lambda target_type, target_number, limit=10: _get_analysis_history(
            repo_id, target_type, target_number, limit
        ),
    )
