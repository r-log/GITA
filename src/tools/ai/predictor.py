"""
AI tools for progress prediction: velocity calculation, completion forecasting, blocker detection.
"""

import json
from datetime import datetime
from openai import AsyncOpenAI

from src.core.config import settings
from src.tools.base import Tool, ToolResult

_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=settings.openrouter_api_key,
)


async def _calculate_velocity(milestone_issues: list[dict]) -> ToolResult:
    """Compute issues-closed-per-day trend from milestone issues."""
    try:
        closed = [i for i in milestone_issues if i.get("state") == "closed" and i.get("closed_at")]
        if not closed:
            return ToolResult(success=True, data={
                "velocity": 0.0,
                "closed_count": 0,
                "total_count": len(milestone_issues),
                "trend": "no_data",
            })

        # Parse close dates and compute velocity
        close_dates = sorted([
            datetime.fromisoformat(i["closed_at"].replace("Z", "+00:00"))
            for i in closed
        ])

        if len(close_dates) < 2:
            days_span = 1
        else:
            days_span = max((close_dates[-1] - close_dates[0]).days, 1)

        velocity = len(closed) / days_span

        # Determine trend (compare first half vs second half)
        mid = len(close_dates) // 2
        if mid > 0:
            first_half_days = max((close_dates[mid] - close_dates[0]).days, 1)
            second_half_days = max((close_dates[-1] - close_dates[mid]).days, 1)
            first_vel = mid / first_half_days
            second_vel = (len(close_dates) - mid) / second_half_days
            if second_vel > first_vel * 1.2:
                trend = "accelerating"
            elif second_vel < first_vel * 0.8:
                trend = "decelerating"
            else:
                trend = "steady"
        else:
            trend = "insufficient_data"

        return ToolResult(success=True, data={
            "velocity": round(velocity, 3),
            "closed_count": len(closed),
            "open_count": len(milestone_issues) - len(closed),
            "total_count": len(milestone_issues),
            "completion_pct": round(len(closed) / len(milestone_issues) * 100, 1) if milestone_issues else 0,
            "trend": trend,
        })
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _predict_completion(
    velocity_data: dict,
    milestone_data: dict,
) -> ToolResult:
    """AI tool: given velocity + remaining issues + due date, predict if milestone will be met on time."""
    try:
        response = await _client.chat.completions.create(
            model=settings.ai_default_model,
            messages=[
                {
                    "role": "system",
                    "content": """You are a project progress analyst. Given velocity data and milestone info, predict whether the milestone will be completed on time.

Respond with JSON:
{
  "on_track": true/false,
  "predicted_completion_date": "YYYY-MM-DD or null if insufficient data",
  "days_remaining_estimate": number or null,
  "risk_level": "low|medium|high|critical",
  "reasoning": "explanation of the prediction",
  "recommendations": ["actionable suggestions"]
}""",
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "velocity": velocity_data,
                        "milestone": {
                            "title": milestone_data.get("title"),
                            "due_on": milestone_data.get("due_on"),
                            "open_issues": milestone_data.get("open_issues"),
                            "closed_issues": milestone_data.get("closed_issues"),
                        },
                        "current_date": datetime.utcnow().isoformat(),
                    }, default=str),
                },
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        return ToolResult(success=True, data=result)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _detect_blockers(issues: list[dict], stale_days: int = 14) -> ToolResult:
    """Find issues with no activity for X days."""
    try:
        now = datetime.utcnow()
        blockers = []
        for issue in issues:
            if issue.get("state") != "open":
                continue
            updated = issue.get("updated_at")
            if not updated:
                continue
            updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None)
            days_stale = (now - updated_dt).days
            if days_stale >= stale_days:
                blockers.append({
                    "number": issue.get("number"),
                    "title": issue.get("title"),
                    "days_stale": days_stale,
                    "assignees": [a.get("login", a) if isinstance(a, dict) else a for a in issue.get("assignees", [])],
                    "labels": [l.get("name", l) if isinstance(l, dict) else l for l in issue.get("labels", [])],
                })

        blockers.sort(key=lambda b: b["days_stale"], reverse=True)
        return ToolResult(success=True, data={
            "blockers": blockers,
            "count": len(blockers),
            "stale_threshold_days": stale_days,
        })
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _detect_stale_prs(pull_requests: list[dict], stale_days: int = 7) -> ToolResult:
    """Find PRs that have been open too long."""
    try:
        now = datetime.utcnow()
        stale = []
        for pr in pull_requests:
            if pr.get("state") != "open":
                continue
            created = pr.get("created_at")
            if not created:
                continue
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00")).replace(tzinfo=None)
            days_open = (now - created_dt).days
            if days_open >= stale_days:
                stale.append({
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "days_open": days_open,
                    "author": pr.get("user", {}).get("login") if isinstance(pr.get("user"), dict) else None,
                })

        stale.sort(key=lambda p: p["days_open"], reverse=True)
        return ToolResult(success=True, data={
            "stale_prs": stale,
            "count": len(stale),
            "stale_threshold_days": stale_days,
        })
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_calculate_velocity() -> Tool:
    return Tool(
        name="calculate_velocity",
        description="Compute issues-closed-per-day velocity trend from a list of milestone issues. Returns velocity, completion %, and trend direction.",
        parameters={
            "type": "object",
            "properties": {
                "milestone_issues": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of issues in the milestone with state and closed_at fields",
                },
            },
            "required": ["milestone_issues"],
        },
        handler=lambda milestone_issues: _calculate_velocity(milestone_issues),
    )


def make_predict_completion() -> Tool:
    return Tool(
        name="predict_completion",
        description="AI tool: Given velocity data and milestone info, predict whether the milestone will be completed on time. Returns prediction with risk level.",
        parameters={
            "type": "object",
            "properties": {
                "velocity_data": {"type": "object", "description": "Output from calculate_velocity"},
                "milestone_data": {"type": "object", "description": "Milestone with title, due_on, open_issues, closed_issues"},
            },
            "required": ["velocity_data", "milestone_data"],
        },
        handler=lambda velocity_data, milestone_data: _predict_completion(velocity_data, milestone_data),
    )


def make_detect_blockers() -> Tool:
    return Tool(
        name="detect_blockers",
        description="Find open issues with no activity for X days (default 14). Returns stale issues sorted by staleness.",
        parameters={
            "type": "object",
            "properties": {
                "issues": {"type": "array", "items": {"type": "object"}, "description": "List of issues to check"},
                "stale_days": {"type": "integer", "description": "Days of inactivity to consider stale (default 14)"},
            },
            "required": ["issues"],
        },
        handler=lambda issues, stale_days=14: _detect_blockers(issues, stale_days),
    )


def make_detect_stale_prs() -> Tool:
    return Tool(
        name="detect_stale_prs",
        description="Find PRs that have been open too long (default 7 days).",
        parameters={
            "type": "object",
            "properties": {
                "pull_requests": {"type": "array", "items": {"type": "object"}, "description": "List of PRs to check"},
                "stale_days": {"type": "integer", "description": "Days open to consider stale (default 7)"},
            },
            "required": ["pull_requests"],
        },
        handler=lambda pull_requests, stale_days=7: _detect_stale_prs(pull_requests, stale_days),
    )
