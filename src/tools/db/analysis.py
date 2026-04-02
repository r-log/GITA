"""
DB tools for analysis persistence: S.M.A.R.T. evaluations, general analyses, progress snapshots.
"""

from datetime import datetime
from sqlalchemy import select, desc

from src.core.database import async_session
from src.models.smart_evaluation import SmartEvaluationModel
from src.models.analysis import Analysis
from src.tools.base import Tool, ToolResult


async def _save_evaluation(
    issue_id: int,
    is_milestone: bool,
    evaluation: dict,
) -> ToolResult:
    try:
        async with async_session() as session:
            record = SmartEvaluationModel(
                issue_id=issue_id,
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
        return ToolResult(success=False, error=str(e))


async def _get_previous_evaluation(issue_id: int) -> ToolResult:
    try:
        async with async_session() as session:
            stmt = (
                select(SmartEvaluationModel)
                .where(SmartEvaluationModel.issue_id == issue_id)
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
        return ToolResult(success=False, error=str(e))


def make_save_evaluation(issue_db_id: int) -> Tool:
    return Tool(
        name="save_evaluation",
        description="Save a S.M.A.R.T. evaluation result to the database.",
        parameters={
            "type": "object",
            "properties": {
                "is_milestone": {"type": "boolean"},
                "evaluation": {"type": "object", "description": "Full evaluation result from evaluate_smart"},
            },
            "required": ["evaluation"],
        },
        handler=lambda evaluation, is_milestone=False: _save_evaluation(issue_db_id, is_milestone, evaluation),
    )


def make_get_previous_evaluation(issue_db_id: int) -> Tool:
    return Tool(
        name="get_previous_evaluation",
        description="Fetch the most recent S.M.A.R.T. evaluation for this issue from the database.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda: _get_previous_evaluation(issue_db_id),
    )


def make_save_analysis(repo_id: int) -> Tool:
    return Tool(
        name="save_analysis",
        description="Save a general analysis result (issue, PR, or milestone) to the database.",
        parameters={
            "type": "object",
            "properties": {
                "target_type": {"type": "string", "enum": ["issue", "pr", "milestone"]},
                "target_number": {"type": "integer", "description": "GitHub issue/PR/milestone number"},
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
        description="Fetch past analysis records for a specific target (issue/PR/milestone) from the database.",
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
