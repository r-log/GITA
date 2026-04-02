"""
GitHub tools for user tagging (mentioning users in comments).
This is a convenience tool — it wraps post_comment with @mentions.
"""

from src.core.github_auth import GitHubClient
from src.tools.base import Tool, ToolResult


async def _tag_user(
    installation_id: int,
    repo_full_name: str,
    issue_number: int,
    usernames: list[str],
    message: str,
) -> ToolResult:
    """Post a comment that @mentions specific users."""
    client = GitHubClient(installation_id)
    try:
        mentions = " ".join(f"@{u}" for u in usernames)
        body = f"{mentions}\n\n{message}"
        data = await client.post(
            f"/repos/{repo_full_name}/issues/{issue_number}/comments",
            json={"body": body},
        )
        return ToolResult(success=True, data={"id": data["id"], "html_url": data["html_url"]})
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_tag_user(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="tag_user",
        description="Post a comment that @mentions specific users to get their attention.",
        parameters={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer", "description": "Issue or PR number"},
                "usernames": {"type": "array", "items": {"type": "string"}, "description": "GitHub usernames to mention"},
                "message": {"type": "string", "description": "Message to include with the mention"},
            },
            "required": ["issue_number", "usernames", "message"],
        },
        handler=lambda issue_number, usernames, message: _tag_user(
            installation_id, repo_full_name, issue_number, usernames, message
        ),
    )
