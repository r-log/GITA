"""
GitHub tools for pull request operations.
"""

from src.core.github_auth import GitHubClient
from src.tools.base import Tool, ToolResult


async def _get_pr(installation_id: int, repo_full_name: str, pr_number: int) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        data = await client.get(f"/repos/{repo_full_name}/pulls/{pr_number}")
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _get_pr_diff(installation_id: int, repo_full_name: str, pr_number: int) -> ToolResult:
    """Fetch the full diff for a PR."""
    client = GitHubClient(installation_id)
    try:
        # Use the accept header for diff format
        token = await client._ensure_token()
        import httpx
        async with httpx.AsyncClient() as http:
            response = await http.get(
                f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.diff",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response.raise_for_status()
            diff = response.text
            # Truncate very large diffs to avoid overwhelming the LLM
            if len(diff) > 50000:
                diff = diff[:50000] + "\n\n... [diff truncated, too large] ..."
            return ToolResult(success=True, data={"diff": diff, "size": len(response.text)})
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _get_pr_files(installation_id: int, repo_full_name: str, pr_number: int) -> ToolResult:
    """List changed files with stats."""
    client = GitHubClient(installation_id)
    try:
        data = await client.get(f"/repos/{repo_full_name}/pulls/{pr_number}/files")
        files = [
            {
                "filename": f["filename"],
                "status": f["status"],  # added, removed, modified, renamed
                "additions": f["additions"],
                "deletions": f["deletions"],
                "changes": f["changes"],
            }
            for f in data
        ]
        return ToolResult(success=True, data=files)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _get_open_prs(installation_id: int, repo_full_name: str) -> ToolResult:
    """List all open pull requests."""
    client = GitHubClient(installation_id)
    try:
        data = await client.get(
            f"/repos/{repo_full_name}/pulls",
            params={"state": "open", "per_page": 100},
        )
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_get_pr(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_pr",
        description="Fetch pull request details (title, body, author, labels, base/head branches).",
        parameters={
            "type": "object",
            "properties": {
                "pr_number": {"type": "integer"},
            },
            "required": ["pr_number"],
        },
        handler=lambda pr_number: _get_pr(installation_id, repo_full_name, pr_number),
    )


def make_get_pr_diff(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_pr_diff",
        description="Fetch the full diff for a pull request. Large diffs are truncated to 50k chars.",
        parameters={
            "type": "object",
            "properties": {
                "pr_number": {"type": "integer"},
            },
            "required": ["pr_number"],
        },
        handler=lambda pr_number: _get_pr_diff(installation_id, repo_full_name, pr_number),
    )


def make_get_pr_files(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_pr_files",
        description="List files changed in a pull request with additions/deletions stats.",
        parameters={
            "type": "object",
            "properties": {
                "pr_number": {"type": "integer"},
            },
            "required": ["pr_number"],
        },
        handler=lambda pr_number: _get_pr_files(installation_id, repo_full_name, pr_number),
    )


def make_get_open_prs(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_open_prs",
        description="List all open pull requests in the repository.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda: _get_open_prs(installation_id, repo_full_name),
    )
