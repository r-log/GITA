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


