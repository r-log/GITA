"""
GitHub tools for pull request operations.
"""

import structlog
from sqlalchemy import select

from src.core.github_auth import GitHubClient
from src.core.database import async_session
from src.models.pull_request import PullRequestModel
from src.models.pr_file_change import PrFileChange
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


async def _get_pr(installation_id: int, repo_full_name: str, pr_number: int) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        data = await client.get(f"/repos/{repo_full_name}/pulls/{pr_number}")
        return ToolResult(success=True, data=data)
    except Exception as e:
        log.warning("get_pr_failed", operation="get_pr", error=str(e), exc_info=True)
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
        log.warning("get_pr_diff_failed", operation="get_pr_diff", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _get_pr_files(
    installation_id: int, repo_full_name: str, pr_number: int, repo_id: int = 0,
) -> ToolResult:
    """List changed files with stats. Persists to pr_file_changes when repo_id is provided."""
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

        # Side-effect: persist file changes for graph impact analysis
        if repo_id and files:
            try:
                await _persist_pr_file_changes(repo_id, pr_number, files)
            except Exception as e:
                log.warning("pr_file_persist_failed", pr=pr_number, error=str(e))

        return ToolResult(success=True, data=files)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _persist_pr_file_changes(repo_id: int, pr_number: int, files: list[dict]) -> None:
    """Persist PR file changes to the database for graph queries."""
    async with async_session() as session:
        # Resolve pr_id from repo_id + pr_number
        result = await session.execute(
            select(PullRequestModel.id).where(
                PullRequestModel.repo_id == repo_id,
                PullRequestModel.github_number == pr_number,
            )
        )
        pr_id = result.scalar_one_or_none()
        if not pr_id:
            return

        for f in files:
            # Upsert: check if record exists
            existing = await session.execute(
                select(PrFileChange).where(
                    PrFileChange.pr_id == pr_id,
                    PrFileChange.file_path == f["filename"],
                )
            )
            record = existing.scalar_one_or_none()

            if record:
                record.change_type = f["status"]
                record.additions = f["additions"]
                record.deletions = f["deletions"]
            else:
                session.add(PrFileChange(
                    repo_id=repo_id,
                    pr_id=pr_id,
                    file_path=f["filename"],
                    change_type=f["status"],
                    additions=f["additions"],
                    deletions=f["deletions"],
                ))

        await session.commit()


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


def make_get_pr_files(installation_id: int, repo_full_name: str, repo_id: int = 0) -> Tool:
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
        handler=lambda pr_number: _get_pr_files(installation_id, repo_full_name, pr_number, repo_id),
    )


def make_get_open_prs(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_open_prs",
        description="List all open pull requests in the repository.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda: _get_open_prs(installation_id, repo_full_name),
    )
