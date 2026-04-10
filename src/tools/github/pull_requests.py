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
from src.tools.db.entity_sync import persist_diff, get_cached_diff, persist_reviews, persist_pr_from_payload

log = structlog.get_logger()


async def _get_pr(installation_id: int, repo_full_name: str, pr_number: int) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        data = await client.get(f"/repos/{repo_full_name}/pulls/{pr_number}")
        return ToolResult(success=True, data=data)
    except Exception as e:
        log.warning("get_pr_failed", operation="get_pr", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _get_pr_diff(
    installation_id: int, repo_full_name: str, pr_number: int,
    repo_id: int = 0, head_sha: str = "",
) -> ToolResult:
    """Fetch the full diff for a PR. Uses DB cache when repo_id and head_sha are provided."""
    # Check cache first
    if repo_id and head_sha:
        cached = await get_cached_diff(repo_id, pr_number, head_sha)
        if cached is not None:
            return ToolResult(success=True, data={"diff": cached, "size": len(cached), "cached": True})

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
            raw_size = len(response.text)
            diff = response.text
            # Truncate very large diffs to avoid overwhelming the LLM
            if len(diff) > 50000:
                diff = diff[:50000] + "\n\n... [diff truncated, too large] ..."

            # Cache the diff for future use
            if repo_id and head_sha:
                try:
                    await persist_diff(repo_id, pr_number, head_sha, diff, raw_size)
                except Exception:
                    pass

            return ToolResult(success=True, data={"diff": diff, "size": raw_size})
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
            # Create a minimal PR record so file changes can be linked
            pr_record = PullRequestModel(repo_id=repo_id, github_number=pr_number)
            session.add(pr_record)
            await session.flush()
            pr_id = pr_record.id

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


async def _get_push_diff(installation_id: int, repo_full_name: str, before: str, after: str) -> ToolResult:
    """Fetch the diff between two commits (for push events that have no PR)."""
    client = GitHubClient(installation_id)
    try:
        data = await client.get(f"/repos/{repo_full_name}/compare/{before}...{after}")
        files = [
            {
                "filename": f["filename"],
                "status": f["status"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "patch": f.get("patch", "")[:5000],  # cap per-file patch size
            }
            for f in data.get("files", [])
        ]
        # Build a combined diff summary
        diff_text = ""
        for f in files:
            if f["patch"]:
                diff_text += f"--- {f['filename']} ({f['status']}: +{f['additions']}/-{f['deletions']})\n"
                diff_text += f["patch"] + "\n\n"

        # Truncate total diff
        if len(diff_text) > 30000:
            diff_text = diff_text[:30000] + "\n\n... [diff truncated] ..."

        return ToolResult(success=True, data={
            "diff": diff_text,
            "files": files,
            "total_commits": data.get("total_commits", 0),
            "ahead_by": data.get("ahead_by", 0),
        })
    except Exception as e:
        log.warning("get_push_diff_failed", error=str(e))
        return ToolResult(success=False, error=str(e))


async def _create_audit_pr(
    installation_id: int, repo_full_name: str,
    before_sha: str, after_sha: str, branch_name: str,
    title: str, body: str,
) -> ToolResult:
    """
    Create a retroactive PR for a direct push.
    Creates a branch at the before-commit, then opens a PR showing the diff.
    """
    client = GitHubClient(installation_id)
    try:
        # 1. Create a branch pointing at the BEFORE commit
        ref_result = await client.post(
            f"/repos/{repo_full_name}/git/refs",
            json={"ref": f"refs/heads/{branch_name}", "sha": before_sha},
        )

        # 2. Open a PR: head=default branch (has the new code), base=audit branch (old state)
        # This shows exactly what the push changed
        default_branch = repo_full_name.split("/")[-1]  # fallback
        try:
            repo_data = await client.get(f"/repos/{repo_full_name}")
            default_branch = repo_data.get("default_branch", "main")
        except Exception:
            default_branch = "main"

        pr_result = await client.post(
            f"/repos/{repo_full_name}/pulls",
            json={
                "title": title,
                "body": body,
                "head": default_branch,
                "base": branch_name,
            },
        )

        return ToolResult(success=True, data={
            "pr_number": pr_result.get("number"),
            "pr_url": pr_result.get("html_url"),
            "branch": branch_name,
        })
    except Exception as e:
        log.warning("create_audit_pr_failed", error=str(e))
        return ToolResult(success=False, error=str(e))


async def _get_pr_reviews(
    installation_id: int, repo_full_name: str, pr_number: int, repo_id: int = 0,
) -> ToolResult:
    """Fetch PR reviews from GitHub and persist them for RAG."""
    client = GitHubClient(installation_id)
    try:
        data = await client.get(f"/repos/{repo_full_name}/pulls/{pr_number}/reviews")
        reviews = [
            {
                "id": r["id"],
                "user": r.get("user"),
                "state": r.get("state", "COMMENTED"),
                "body": r.get("body"),
                "submitted_at": r.get("submitted_at"),
            }
            for r in data
        ]

        # Side-effect: persist reviews for RAG
        if repo_id and reviews:
            try:
                await persist_reviews(repo_id, pr_number, reviews)
            except Exception:
                pass

        return ToolResult(success=True, data=reviews)
    except Exception as e:
        log.warning("get_pr_reviews_failed", pr=pr_number, error=str(e))
        return ToolResult(success=False, error=str(e))


def make_get_open_prs(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_open_prs",
        description="List all open pull requests in the repository.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda: _get_open_prs(installation_id, repo_full_name),
    )
