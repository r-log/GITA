"""
GitHub tools for milestone operations.
"""

import structlog

from src.core.github_auth import GitHubClient
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


async def _get_all_milestones(installation_id: int, repo_full_name: str, state: str = "open") -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        data = await client.get(
            f"/repos/{repo_full_name}/milestones",
            params={"state": state, "per_page": 100},
        )
        return ToolResult(success=True, data=data)
    except Exception as e:
        log.warning("get_all_milestones_failed", operation="get_all_milestones", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _get_milestone(installation_id: int, repo_full_name: str, milestone_number: int) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        data = await client.get(f"/repos/{repo_full_name}/milestones/{milestone_number}")
        return ToolResult(success=True, data=data)
    except Exception as e:
        log.warning("get_milestone_failed", operation="get_milestone", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _create_milestone(
    installation_id: int,
    repo_full_name: str,
    title: str,
    description: str = "",
    due_on: str | None = None,
    state: str = "open",
) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        payload = {"title": title, "description": description, "state": state}
        if due_on:
            payload["due_on"] = due_on  # ISO 8601 format
        data = await client.post(f"/repos/{repo_full_name}/milestones", json=payload)
        return ToolResult(success=True, data=data)
    except Exception as e:
        log.warning("create_milestone_failed", operation="create_milestone", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _update_milestone(
    installation_id: int,
    repo_full_name: str,
    milestone_number: int,
    title: str | None = None,
    description: str | None = None,
    due_on: str | None = None,
    state: str | None = None,
) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        payload = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if due_on is not None:
            payload["due_on"] = due_on
        if state is not None:
            payload["state"] = state
        data = await client.patch(f"/repos/{repo_full_name}/milestones/{milestone_number}", json=payload)
        return ToolResult(success=True, data=data)
    except Exception as e:
        log.warning("update_milestone_failed", operation="update_milestone", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_get_all_milestones(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_all_milestones",
        description="Fetch all milestones in the repository. Returns open milestones by default.",
        parameters={
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
            },
            "required": [],
        },
        handler=lambda state="open": _get_all_milestones(installation_id, repo_full_name, state),
    )


def make_get_milestone(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_milestone",
        description="Fetch a single milestone by number.",
        parameters={
            "type": "object",
            "properties": {
                "milestone_number": {"type": "integer"},
            },
            "required": ["milestone_number"],
        },
        handler=lambda milestone_number: _get_milestone(installation_id, repo_full_name, milestone_number),
    )


def make_create_milestone(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="create_milestone",
        description="Create a new milestone in the repository.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Milestone title"},
                "description": {"type": "string", "description": "Milestone description"},
                "due_on": {"type": "string", "description": "Due date in ISO 8601 format (e.g. 2026-05-01T00:00:00Z)"},
            },
            "required": ["title"],
        },
        handler=lambda title, description="", due_on=None: _create_milestone(
            installation_id, repo_full_name, title, description, due_on
        ),
    )


def make_update_milestone(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="update_milestone",
        description="Update an existing milestone's title, description, due date, or state.",
        parameters={
            "type": "object",
            "properties": {
                "milestone_number": {"type": "integer"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "due_on": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed"]},
            },
            "required": ["milestone_number"],
        },
        handler=lambda milestone_number, title=None, description=None, due_on=None, state=None: _update_milestone(
            installation_id, repo_full_name, milestone_number, title, description, due_on, state
        ),
    )
