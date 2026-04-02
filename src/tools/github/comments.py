"""
GitHub tools for comment operations.
"""

from src.core.github_auth import GitHubClient
from src.tools.base import Tool, ToolResult


async def _post_comment(installation_id: int, repo_full_name: str, issue_number: int, body: str) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        data = await client.post(
            f"/repos/{repo_full_name}/issues/{issue_number}/comments",
            json={"body": body},
        )
        return ToolResult(success=True, data={"id": data["id"], "html_url": data["html_url"]})
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _edit_comment(installation_id: int, repo_full_name: str, comment_id: int, body: str) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        data = await client.patch(
            f"/repos/{repo_full_name}/issues/comments/{comment_id}",
            json={"body": body},
        )
        return ToolResult(success=True, data={"id": data["id"], "html_url": data["html_url"]})
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_post_comment(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="post_comment",
        description="Post a comment on an issue or pull request.",
        parameters={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer", "description": "Issue or PR number"},
                "body": {"type": "string", "description": "Comment body (markdown)"},
            },
            "required": ["issue_number", "body"],
        },
        handler=lambda issue_number, body: _post_comment(installation_id, repo_full_name, issue_number, body),
    )


def make_edit_comment(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="edit_comment",
        description="Edit an existing comment.",
        parameters={
            "type": "object",
            "properties": {
                "comment_id": {"type": "integer", "description": "The comment ID to edit"},
                "body": {"type": "string", "description": "New comment body (markdown)"},
            },
            "required": ["comment_id", "body"],
        },
        handler=lambda comment_id, body: _edit_comment(installation_id, repo_full_name, comment_id, body),
    )
