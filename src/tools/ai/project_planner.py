"""
AI tools for project planning: infer milestones/tasks from repo, compare plan vs state.
These tools call the LLM to do the heavy reasoning.
"""

import json
from openai import AsyncOpenAI
from thefuzz import fuzz

from src.core.config import settings
from src.tools.base import Tool, ToolResult

_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=settings.openrouter_api_key,
)


async def _infer_project_plan(repo_snapshot: dict) -> ToolResult:
    """Given a repo snapshot (tree + key files), infer milestones and tasks."""
    try:
        response = await _client.chat.completions.create(
            model=settings.ai_default_model,
            messages=[
                {
                    "role": "system",
                    "content": """You are a project planning expert. Given a repository snapshot, infer a structured project plan with milestones and sub-tasks.

Rules:
- Each milestone should represent a coherent feature area or deliverable
- Each task should map to specific files or directories in the repo
- Include estimated effort (small/medium/large) for each task
- If the project already looks well-organized, reflect that — don't invent work
- Be conservative: only suggest milestones for things that clearly need tracking

Respond with JSON:
{
  "project_summary": "Brief description of what the project is",
  "milestones": [
    {
      "title": "Milestone Name",
      "description": "What this milestone covers",
      "tasks": [
        {
          "title": "Task title",
          "description": "What needs to be done",
          "files": ["src/foo.py", "src/bar.py"],
          "effort": "small|medium|large",
          "labels": ["enhancement"]
        }
      ],
      "confidence": 0.0-1.0
    }
  ]
}""",
                },
                {
                    "role": "user",
                    "content": json.dumps(repo_snapshot, default=str),
                },
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        plan = json.loads(response.choices[0].message.content)
        return ToolResult(success=True, data=plan)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _compare_plan_vs_state(suggested_plan: dict, existing_state: dict) -> ToolResult:
    """Compare AI-suggested plan with existing milestones/issues to produce an action list."""
    try:
        response = await _client.chat.completions.create(
            model=settings.ai_default_model,
            messages=[
                {
                    "role": "system",
                    "content": """You are a project reconciliation expert. Compare a suggested project plan with the existing state of milestones and issues on GitHub.

Rules:
- NEVER suggest deleting anything
- If a suggested milestone matches an existing one (similar title/scope), suggest UPDATE, not CREATE
- If an existing issue clearly belongs to a suggested milestone, suggest ASSIGN
- Rate your confidence for each action
- Be conservative — when unsure, suggest flagging for human review

Respond with JSON:
{
  "actions": [
    {
      "type": "create_milestone|update_milestone|create_issue|update_issue|assign_issue|flag|skip",
      "target": "milestone or issue title",
      "details": "what to do",
      "confidence": 0.0-1.0,
      "reason": "why this action"
    }
  ],
  "summary": "Overall reconciliation summary"
}""",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"suggested_plan": suggested_plan, "existing_state": existing_state},
                        default=str,
                    ),
                },
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)
        return ToolResult(success=True, data=result)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _fuzzy_match_milestone(title: str, existing_milestones: list[dict]) -> ToolResult:
    """Fuzzy-match a suggested milestone title against existing milestone titles."""
    best_match = None
    best_score = 0

    for milestone in existing_milestones:
        score = fuzz.ratio(title.lower(), milestone.get("title", "").lower())
        if score > best_score:
            best_score = score
            best_match = milestone

    if best_score >= 80:
        return ToolResult(
            success=True,
            data={"match": best_match, "score": best_score, "action": "update"},
        )
    elif best_score >= 50:
        return ToolResult(
            success=True,
            data={"match": best_match, "score": best_score, "action": "review"},
        )
    else:
        return ToolResult(
            success=True,
            data={"match": None, "score": best_score, "action": "create"},
        )


def make_infer_project_plan() -> Tool:
    return Tool(
        name="infer_project_plan",
        description="AI tool: Given a repository snapshot (file tree + key file contents), infer a structured project plan with milestones and sub-tasks.",
        parameters={
            "type": "object",
            "properties": {
                "repo_snapshot": {
                    "type": "object",
                    "description": "Object with 'tree' (file list) and 'files' (key file contents) from the repo scan",
                },
            },
            "required": ["repo_snapshot"],
        },
        handler=lambda repo_snapshot: _infer_project_plan(repo_snapshot),
    )


def make_compare_plan_vs_state() -> Tool:
    return Tool(
        name="compare_plan_vs_state",
        description="AI tool: Compare a suggested project plan with existing milestones/issues and produce a reconciliation action list.",
        parameters={
            "type": "object",
            "properties": {
                "suggested_plan": {"type": "object", "description": "AI-inferred project plan"},
                "existing_state": {
                    "type": "object",
                    "description": "Current state: existing milestones and issues",
                },
            },
            "required": ["suggested_plan", "existing_state"],
        },
        handler=lambda suggested_plan, existing_state: _compare_plan_vs_state(suggested_plan, existing_state),
    )


def make_fuzzy_match_milestone() -> Tool:
    return Tool(
        name="fuzzy_match_milestone",
        description="Fuzzy-match a milestone title against existing milestones. Returns match score and recommended action (update/review/create).",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Suggested milestone title to match"},
                "existing_milestones": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of existing milestones with 'title' and 'number' fields",
                },
            },
            "required": ["title", "existing_milestones"],
        },
        handler=lambda title, existing_milestones: _fuzzy_match_milestone(title, existing_milestones),
    )
