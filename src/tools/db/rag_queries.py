"""
RAG query tools — give agents searchable access to stored activity data.

Each function has a _internal() for direct use and a make_*() factory
that returns a Tool for agent registration.
"""

import structlog
from sqlalchemy import select, desc, or_, cast, String

from src.core.database import async_session
from src.models.event import EventModel
from src.models.commit import CommitModel
from src.models.comment import CommentModel
from src.models.review import ReviewModel
from src.models.diff import DiffModel
from src.models.issue import IssueModel
from src.models.pull_request import PullRequestModel
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


# ── Search Events ─────────────────────────────────────────────────


async def _search_events(
    repo_id: int,
    event_type: str | None = None,
    target_type: str | None = None,
    target_number: int | None = None,
    sender: str | None = None,
    limit: int = 20,
) -> ToolResult:
    """Search the webhook event log. Returns summaries (not full payloads)."""
    try:
        async with async_session() as session:
            stmt = select(EventModel).where(EventModel.repo_id == repo_id)
            if event_type:
                stmt = stmt.where(EventModel.event_type == event_type)
            if target_type:
                stmt = stmt.where(EventModel.target_type == target_type)
            if target_number:
                stmt = stmt.where(EventModel.target_number == target_number)
            if sender:
                stmt = stmt.where(EventModel.sender_login == sender)
            stmt = stmt.order_by(desc(EventModel.received_at)).limit(min(limit, 50))

            result = await session.execute(stmt)
            events = result.scalars().all()

        data = [
            {
                "event_type": e.event_type,
                "action": e.action,
                "sender": e.sender_login,
                "target_type": e.target_type,
                "target_number": e.target_number,
                "received_at": e.received_at.isoformat() if e.received_at else None,
            }
            for e in events
        ]
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_search_events(repo_id: int) -> Tool:
    return Tool(
        name="search_events",
        description="Search the webhook event history. Find what happened to an issue/PR, "
                    "recent activity by a user, or events of a specific type.",
        parameters={
            "type": "object",
            "properties": {
                "event_type": {"type": "string", "description": "Filter by event type: issues, pull_request, push, issue_comment"},
                "target_type": {"type": "string", "description": "Filter by target: issue, pr, push"},
                "target_number": {"type": "integer", "description": "Filter by issue/PR number"},
                "sender": {"type": "string", "description": "Filter by sender GitHub login"},
                "limit": {"type": "integer", "description": "Max results (default 20, max 50)"},
            },
        },
        handler=lambda event_type=None, target_type=None, target_number=None, sender=None, limit=20: _search_events(
            repo_id, event_type, target_type, target_number, sender, limit
        ),
    )


# ── Search Commits ────────────────────────────────────────────────


async def _search_commits(
    repo_id: int,
    author: str | None = None,
    file_path: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
) -> ToolResult:
    """Search commit history. Can filter by author, file path, or message keyword."""
    try:
        async with async_session() as session:
            stmt = select(CommitModel).where(CommitModel.repo_id == repo_id)
            if author:
                stmt = stmt.where(CommitModel.author_login == author)
            if keyword:
                stmt = stmt.where(CommitModel.message.ilike(f"%{keyword}%"))
            stmt = stmt.order_by(desc(CommitModel.committed_at)).limit(min(limit, 50))

            result = await session.execute(stmt)
            commits = result.scalars().all()

        data = []
        for c in commits:
            # If filtering by file_path, check JSONB arrays
            if file_path:
                all_files = (c.files_added or []) + (c.files_modified or []) + (c.files_removed or [])
                if not any(file_path in f for f in all_files):
                    continue

            data.append({
                "sha": c.sha[:10],
                "message": c.message[:200],
                "author": c.author_login or c.author_name,
                "committed_at": c.committed_at.isoformat() if c.committed_at else None,
                "files_added": len(c.files_added or []),
                "files_modified": len(c.files_modified or []),
                "files_removed": len(c.files_removed or []),
            })

        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_search_commits(repo_id: int) -> Tool:
    return Tool(
        name="search_commits",
        description="Search commit history. Find who changed a file, commits by an author, "
                    "or commits matching a keyword in the message.",
        parameters={
            "type": "object",
            "properties": {
                "author": {"type": "string", "description": "GitHub login of the commit author"},
                "file_path": {"type": "string", "description": "File path to search for in changed files"},
                "keyword": {"type": "string", "description": "Search term in commit messages"},
                "limit": {"type": "integer", "description": "Max results (default 20, max 50)"},
            },
        },
        handler=lambda author=None, file_path=None, keyword=None, limit=20: _search_commits(
            repo_id, author, file_path, keyword, limit
        ),
    )


# ── Search Comments ───────────────────────────────────────────────


async def _search_comments(
    repo_id: int,
    target_type: str | None = None,
    target_number: int | None = None,
    author: str | None = None,
    keyword: str | None = None,
    include_bot: bool = False,
    limit: int = 20,
) -> ToolResult:
    """Search issue/PR comments. Can filter by target, author, or keyword."""
    try:
        async with async_session() as session:
            stmt = select(CommentModel).where(CommentModel.repo_id == repo_id)
            if target_type:
                stmt = stmt.where(CommentModel.target_type == target_type)
            if target_number:
                stmt = stmt.where(CommentModel.target_number == target_number)
            if author:
                stmt = stmt.where(CommentModel.author_login == author)
            if keyword:
                stmt = stmt.where(CommentModel.body.ilike(f"%{keyword}%"))
            if not include_bot:
                stmt = stmt.where(CommentModel.is_bot == False)
            stmt = stmt.order_by(desc(CommentModel.github_created_at)).limit(min(limit, 50))

            result = await session.execute(stmt)
            comments = result.scalars().all()

        data = [
            {
                "target_type": c.target_type,
                "target_number": c.target_number,
                "author": c.author_login,
                "body": (c.body or "")[:500],
                "created_at": c.github_created_at.isoformat() if c.github_created_at else None,
            }
            for c in comments
        ]
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_search_comments(repo_id: int) -> Tool:
    return Tool(
        name="search_comments",
        description="Search issue/PR comment history. Find discussions about a topic, "
                    "comments by a user, or all comments on a specific issue/PR.",
        parameters={
            "type": "object",
            "properties": {
                "target_type": {"type": "string", "description": "Filter by: issue or pr"},
                "target_number": {"type": "integer", "description": "Issue or PR number"},
                "author": {"type": "string", "description": "GitHub login of comment author"},
                "keyword": {"type": "string", "description": "Search term in comment body"},
                "include_bot": {"type": "boolean", "description": "Include bot comments (default false)"},
                "limit": {"type": "integer", "description": "Max results (default 20, max 50)"},
            },
        },
        handler=lambda target_type=None, target_number=None, author=None, keyword=None, include_bot=False, limit=20: _search_comments(
            repo_id, target_type, target_number, author, keyword, include_bot, limit
        ),
    )


# ── Get PR Reviews ────────────────────────────────────────────────


async def _get_pr_reviews(repo_id: int, pr_number: int) -> ToolResult:
    """Get all stored reviews for a PR."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(ReviewModel)
                .where(ReviewModel.repo_id == repo_id, ReviewModel.pr_number == pr_number)
                .order_by(ReviewModel.submitted_at)
            )
            reviews = result.scalars().all()

        data = [
            {
                "author": r.author_login,
                "state": r.state,
                "body": (r.body or "")[:500],
                "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
            }
            for r in reviews
        ]
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_get_pr_reviews(repo_id: int) -> Tool:
    return Tool(
        name="get_pr_reviews",
        description="Get all reviews for a pull request — shows who approved, "
                    "requested changes, or left comments.",
        parameters={
            "type": "object",
            "properties": {
                "pr_number": {"type": "integer", "description": "PR number"},
            },
            "required": ["pr_number"],
        },
        handler=lambda pr_number: _get_pr_reviews(repo_id, pr_number),
    )


# ── Get Full Issue ────────────────────────────────────────────────


async def _get_issue_full(repo_id: int, github_number: int) -> ToolResult:
    """Get the enriched issue record with body, author, and timestamps."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(IssueModel).where(
                    IssueModel.repo_id == repo_id,
                    IssueModel.github_number == github_number,
                )
            )
            issue = result.scalar_one_or_none()

        if not issue:
            return ToolResult(success=True, data=None)

        data = {
            "number": issue.github_number,
            "title": issue.title,
            "state": issue.state,
            "body": issue.body,
            "author": issue.author,
            "labels": issue.labels,
            "assignees": issue.assignees,
            "is_milestone_tracker": issue.is_milestone_tracker,
            "linked_issue_numbers": issue.linked_issue_numbers,
            "created_at": issue.github_created_at.isoformat() if issue.github_created_at else None,
            "closed_at": issue.closed_at.isoformat() if issue.closed_at else None,
        }
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_get_issue_full(repo_id: int) -> Tool:
    return Tool(
        name="get_issue_full",
        description="Get full issue details from the local database including body text, "
                    "author, timestamps, and metadata. Faster than the GitHub API.",
        parameters={
            "type": "object",
            "properties": {
                "github_number": {"type": "integer", "description": "Issue number"},
            },
            "required": ["github_number"],
        },
        handler=lambda github_number: _get_issue_full(repo_id, github_number),
    )


# ── Get Full PR ───────────────────────────────────────────────────


async def _get_pr_full(repo_id: int, github_number: int) -> ToolResult:
    """Get the enriched PR record with body, branches, and merge info."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(PullRequestModel).where(
                    PullRequestModel.repo_id == repo_id,
                    PullRequestModel.github_number == github_number,
                )
            )
            pr = result.scalar_one_or_none()

        if not pr:
            return ToolResult(success=True, data=None)

        data = {
            "number": pr.github_number,
            "title": pr.title,
            "state": pr.state,
            "body": pr.body,
            "author": pr.author,
            "base_branch": pr.base_branch,
            "head_branch": pr.head_branch,
            "commit_count": pr.commit_count,
            "diff_size": pr.diff_size,
            "files_changed": pr.files_changed,
            "risk_level": pr.risk_level,
            "created_at": pr.github_created_at.isoformat() if pr.github_created_at else None,
            "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
            "merged_by": pr.merged_by,
        }
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_get_pr_full(repo_id: int) -> Tool:
    return Tool(
        name="get_pr_full",
        description="Get full PR details from the local database including body text, "
                    "branches, merge info, and risk level. Faster than the GitHub API.",
        parameters={
            "type": "object",
            "properties": {
                "github_number": {"type": "integer", "description": "PR number"},
            },
            "required": ["github_number"],
        },
        handler=lambda github_number: _get_pr_full(repo_id, github_number),
    )


# ── Get Parent Trackers ───────────────────────────────────────────

from src.utils.checklist import CHECKLIST_ITEM_RE


async def _get_parent_trackers(repo_id: int, issue_number: int) -> ToolResult:
    """
    Find every Milestone Tracker issue whose checklist references the given
    sub-issue number. Returns the tracker's body and a parsed checklist state
    so the LLM can decide how to edit it.

    Use this when a sub-issue is closed / completed, to find which tracker(s)
    need their checkbox ticked.
    """
    try:
        async with async_session() as session:
            # Prefer the structured linked_issue_numbers when it's populated.
            # Fall back to a LIKE body scan otherwise (some trackers were
            # created before this field was set reliably).
            stmt = select(IssueModel).where(
                IssueModel.repo_id == repo_id,
                IssueModel.is_milestone_tracker == True,  # noqa: E712
            )
            result = await session.execute(stmt)
            trackers = result.scalars().all()

        matching = []
        for t in trackers:
            linked = t.linked_issue_numbers or []
            body = t.body or ""
            mentions = f"#{issue_number}" in body or issue_number in linked
            if not mentions:
                continue

            # Parse the checklist to expose current state
            checklist = []
            for mark, desc, num_str in CHECKLIST_ITEM_RE.findall(body):
                num = int(num_str)
                checklist.append({
                    "sub_issue_number": num,
                    "description": desc.strip(),
                    "checked": mark.lower() == "x",
                    "is_target": num == issue_number,
                })

            completed = sum(1 for c in checklist if c["checked"])
            total = len(checklist)

            matching.append({
                "tracker_number": t.github_number,
                "title": t.title,
                "state": t.state,
                "body": body,
                "checklist": checklist,
                "progress": {
                    "completed": completed,
                    "total": total,
                    "pct": round(completed / total * 100) if total else 0,
                },
                "target_item_checked": any(
                    c["checked"] for c in checklist if c["is_target"]
                ),
            })

        return ToolResult(success=True, data=matching)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def make_get_parent_trackers(repo_id: int) -> Tool:
    return Tool(
        name="get_parent_trackers",
        description=(
            "Find every Milestone Tracker issue whose checklist references a given "
            "sub-issue number. Returns each tracker's body, parsed checklist state "
            "(which items are checked vs unchecked), progress percentage, and whether "
            "the target item is already checked. Use this when you're about to close "
            "a sub-issue and want to tick it off in the parent tracker, or when you "
            "need to decide whether a parent tracker is now 100% complete and should "
            "itself be closed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "issue_number": {
                    "type": "integer",
                    "description": "The sub-issue number to look up parent trackers for",
                },
            },
            "required": ["issue_number"],
        },
        handler=lambda issue_number: _get_parent_trackers(repo_id, issue_number),
    )
