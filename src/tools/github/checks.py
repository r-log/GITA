"""
GitHub tools for check run operations.
"""

import structlog

from src.core.github_auth import GitHubClient
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


async def _create_check_run(
    installation_id: int,
    repo_full_name: str,
    name: str,
    head_sha: str,
    status: str = "completed",
    conclusion: str = "neutral",
    title: str = "",
    summary: str = "",
    text: str = "",
) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        payload = {
            "name": name,
            "head_sha": head_sha,
            "status": status,
            "conclusion": conclusion,
            "output": {
                "title": title or name,
                "summary": summary,
                "text": text,
            },
        }
        data = await client.post(f"/repos/{repo_full_name}/check-runs", json=payload)
        return ToolResult(success=True, data={"id": data["id"], "html_url": data.get("html_url")})
    except Exception as e:
        log.warning("create_check_run_failed", operation="create_check_run", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_create_check_run(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="create_check_run",
        description="Create a GitHub check run (pass/fail/neutral) on a commit. Used for PR review results.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Check run name (e.g. 'GitHub Assistant / PR Review')"},
                "head_sha": {"type": "string", "description": "The SHA of the commit to attach the check to"},
                "conclusion": {
                    "type": "string",
                    "enum": ["action_required", "cancelled", "failure", "neutral", "success", "skipped", "timed_out"],
                    "description": "Check run conclusion",
                },
                "title": {"type": "string", "description": "Title shown in the check run output"},
                "summary": {"type": "string", "description": "Summary markdown shown in check output"},
                "text": {"type": "string", "description": "Detailed text/markdown for the check output"},
            },
            "required": ["name", "head_sha", "conclusion"],
        },
        handler=lambda name, head_sha, conclusion, title="", summary="", text="": _create_check_run(
            installation_id, repo_full_name, name, head_sha, "completed", conclusion, title, summary, text
        ),
    )
