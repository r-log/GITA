"""
AI tools for S.M.A.R.T. evaluation and milestone alignment checking.
"""

import json
import structlog
from src.core.config import settings
from src.core.llm_client import llm_json_call
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


async def _evaluate_smart(issue_data: dict, linked_issues: list[dict] | None = None) -> ToolResult:
    """Evaluate an issue or milestone against S.M.A.R.T. criteria using AI."""
    try:
        is_milestone = issue_data.get("is_milestone", False)
        issue_type = "Milestone" if is_milestone else "Issue"

        context = {
            "type": issue_type,
            "title": issue_data.get("title", ""),
            "body": issue_data.get("body", ""),
            "labels": issue_data.get("labels", []),
            "assignees": issue_data.get("assignees", []),
            "milestone": issue_data.get("milestone"),
            "state": issue_data.get("state", "open"),
            "created_at": issue_data.get("created_at"),
            "updated_at": issue_data.get("updated_at"),
        }
        if linked_issues:
            context["linked_issues"] = [
                {"number": i.get("number"), "title": i.get("title"), "state": i.get("state")}
                for i in linked_issues
            ]

        result = await llm_json_call(
            model=settings.ai_model_smart_evaluator,
            messages=[
                {
                    "role": "system",
                    "content": f"""You are a project quality analyst. Evaluate this GitHub {issue_type} against S.M.A.R.T. criteria.

For each criterion, provide:
- score: 0.0 to 1.0
- findings: what was found (list of strings)
- suggestions: improvement recommendations (list of strings)
- missing_elements: what's missing (list of strings)

Respond with JSON:
{{
  "specific": {{"score": 0.0, "findings": [], "suggestions": [], "missing_elements": []}},
  "measurable": {{"score": 0.0, "findings": [], "suggestions": [], "missing_elements": []}},
  "achievable": {{"score": 0.0, "findings": [], "suggestions": [], "missing_elements": []}},
  "relevant": {{"score": 0.0, "findings": [], "suggestions": [], "missing_elements": []}},
  "time_bound": {{"score": 0.0, "findings": [], "suggestions": [], "missing_elements": []}},
  "overall_score": 0.0,
  "priority_improvements": ["most impactful suggestion first"],
  "action_items": ["concrete actionable items"]
}}""",
                },
                {"role": "user", "content": json.dumps(context, default=str)},
            ],
            caller="evaluate_smart",
        )
        if result is None:
            return ToolResult(success=False, error="S.M.A.R.T. evaluation failed after retries")
        return ToolResult(success=True, data=result)
    except Exception as e:
        log.warning("smart_evaluator_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _check_milestone_alignment(issue_data: dict, milestone_data: dict) -> ToolResult:
    """Check if an issue actually belongs to its assigned milestone."""
    try:
        result = await llm_json_call(
            model=settings.ai_model_milestone_alignment,
            messages=[
                {
                    "role": "system",
                    "content": """You are a project alignment checker. Determine if this issue belongs to its assigned milestone.

Respond with JSON:
{
  "aligned": true/false,
  "confidence": 0.0-1.0,
  "reasoning": "why it does or doesn't align",
  "suggested_milestone": "if misaligned, suggest a better milestone or null",
  "recommendation": "what to do about it"
}""",
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "issue": {
                            "title": issue_data.get("title"),
                            "body": issue_data.get("body"),
                            "labels": issue_data.get("labels", []),
                        },
                        "milestone": {
                            "title": milestone_data.get("title"),
                            "description": milestone_data.get("description"),
                        },
                    }),
                },
            ],
            caller="check_milestone_alignment",
        )
        if result is None:
            return ToolResult(success=False, error="Milestone alignment check failed after retries")
        return ToolResult(success=True, data=result)
    except Exception as e:
        log.warning("smart_evaluator_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_evaluate_smart() -> Tool:
    return Tool(
        name="evaluate_smart",
        description="Evaluate an issue against S.M.A.R.T. criteria (Specific, Measurable, Achievable, Relevant, Time-bound). Returns per-criterion scores and suggestions.",
        parameters={
            "type": "object",
            "properties": {
                "issue_data": {"type": "object", "description": "Issue data (title, body, labels, etc.)"},
                "linked_issues": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Related issues for context",
                },
            },
            "required": ["issue_data"],
        },
        handler=lambda issue_data, linked_issues=None: _evaluate_smart(issue_data, linked_issues),
    )


def make_check_milestone_alignment() -> Tool:
    return Tool(
        name="check_milestone_alignment",
        description="Check if an issue actually belongs to its assigned milestone. Returns alignment score and recommendation.",
        parameters={
            "type": "object",
            "properties": {
                "issue_data": {"type": "object"},
                "milestone_data": {"type": "object"},
            },
            "required": ["issue_data", "milestone_data"],
        },
        handler=lambda issue_data, milestone_data: _check_milestone_alignment(issue_data, milestone_data),
    )
