"""
GitHub tools for label operations.
"""

import structlog

from src.core.github_auth import GitHubClient
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


async def _add_label(installation_id: int, repo_full_name: str, issue_number: int, labels: list[str]) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        data = await client.post(
            f"/repos/{repo_full_name}/issues/{issue_number}/labels",
            json={"labels": labels},
        )
        return ToolResult(success=True, data=data)
    except Exception as e:
        log.warning("add_label_failed", operation="add_label", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _create_label(
    installation_id: int, repo_full_name: str, name: str, color: str = "ededed", description: str = ""
) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        data = await client.post(
            f"/repos/{repo_full_name}/labels",
            json={"name": name, "color": color, "description": description},
        )
        return ToolResult(success=True, data=data)
    except Exception as e:
        log.warning("create_label_failed", operation="create_label", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_add_label(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="add_label",
        description="Add one or more labels to an issue.",
        parameters={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer"},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "Label names to add"},
            },
            "required": ["issue_number", "labels"],
        },
        handler=lambda issue_number, labels: _add_label(installation_id, repo_full_name, issue_number, labels),
    )


def make_create_label(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="create_label",
        description="Create a new label in the repository.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Label name"},
                "color": {"type": "string", "description": "Color hex code without # (e.g. 'ff0000')"},
                "description": {"type": "string", "description": "Label description"},
            },
            "required": ["name"],
        },
        handler=lambda name, color="ededed", description="": _create_label(
            installation_id, repo_full_name, name, color, description
        ),
    )
