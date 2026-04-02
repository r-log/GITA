"""
GitHub tools for issue operations.
"""

from src.core.github_auth import GitHubClient
from src.tools.base import Tool, ToolResult


async def _get_issue(installation_id: int, repo_full_name: str, issue_number: int) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        data = await client.get(f"/repos/{repo_full_name}/issues/{issue_number}")
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _get_all_issues(installation_id: int, repo_full_name: str, state: str = "open") -> ToolResult:
    """Fetch all issues (paginated)."""
    client = GitHubClient(installation_id)
    try:
        all_issues = []
        page = 1
        while True:
            data = await client.get(
                f"/repos/{repo_full_name}/issues",
                params={"state": state, "per_page": 100, "page": page},
            )
            if not data:
                break
            # Filter out pull requests (GitHub API returns PRs in issues endpoint)
            issues = [i for i in data if "pull_request" not in i]
            all_issues.extend(issues)
            if len(data) < 100:
                break
            page += 1
        return ToolResult(success=True, data=all_issues)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _create_issue(
    installation_id: int,
    repo_full_name: str,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
    milestone: int | None = None,
    assignees: list[str] | None = None,
) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if milestone:
            payload["milestone"] = milestone
        if assignees:
            payload["assignees"] = assignees
        data = await client.post(f"/repos/{repo_full_name}/issues", json=payload)
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


async def _update_issue(
    installation_id: int,
    repo_full_name: str,
    issue_number: int,
    title: str | None = None,
    body: str | None = None,
    labels: list[str] | None = None,
    milestone: int | None = None,
    assignees: list[str] | None = None,
    state: str | None = None,
) -> ToolResult:
    client = GitHubClient(installation_id)
    try:
        payload = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if labels is not None:
            payload["labels"] = labels
        if milestone is not None:
            payload["milestone"] = milestone
        if assignees is not None:
            payload["assignees"] = assignees
        if state is not None:
            payload["state"] = state
        data = await client.patch(f"/repos/{repo_full_name}/issues/{issue_number}", json=payload)
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_get_issue(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_issue",
        description="Fetch a single issue by number, including its title, body, labels, assignees, and milestone.",
        parameters={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer", "description": "The issue number"},
            },
            "required": ["issue_number"],
        },
        handler=lambda issue_number: _get_issue(installation_id, repo_full_name, issue_number),
    )


def make_get_all_issues(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_all_issues",
        description="Fetch all issues in the repository. Returns open issues by default.",
        parameters={
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Issue state filter"},
            },
            "required": [],
        },
        handler=lambda state="open": _get_all_issues(installation_id, repo_full_name, state),
    )


def make_create_issue(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="create_issue",
        description="Create a new issue in the repository.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Issue title"},
                "body": {"type": "string", "description": "Issue body (markdown)"},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels to add"},
                "milestone": {"type": "integer", "description": "Milestone number to assign to"},
                "assignees": {"type": "array", "items": {"type": "string"}, "description": "GitHub usernames to assign"},
            },
            "required": ["title"],
        },
        handler=lambda title, body="", labels=None, milestone=None, assignees=None: _create_issue(
            installation_id, repo_full_name, title, body, labels, milestone, assignees
        ),
    )


def make_update_issue(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="update_issue",
        description="Update an existing issue's title, body, labels, milestone, assignees, or state.",
        parameters={
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer", "description": "The issue number to update"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "milestone": {"type": "integer"},
                "assignees": {"type": "array", "items": {"type": "string"}},
                "state": {"type": "string", "enum": ["open", "closed"]},
            },
            "required": ["issue_number"],
        },
        handler=lambda issue_number, title=None, body=None, labels=None, milestone=None, assignees=None, state=None: _update_issue(
            installation_id, repo_full_name, issue_number, title, body, labels, milestone, assignees, state
        ),
    )
