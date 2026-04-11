"""
DB tools for agents to query the code index and save issue records.

These tools give agents RAG-style access to the stored code knowledge base
instead of reading files from GitHub every time.
"""

import json
import structlog

from sqlalchemy import select

log = structlog.get_logger()

from src.core.database import async_session
from src.models.code_index import CodeIndex
from src.models.issue import IssueModel
from src.tools.base import Tool, ToolResult


# ── Query Code Index ───────────────────────────────────────────────

async def _query_code_index(
    repo_id: int,
    file_path: str | None = None,
    language: str | None = None,
    search: str | None = None,
) -> ToolResult:
    """Query the code index for file structure information."""
    try:
        async with async_session() as session:
            stmt = select(CodeIndex).where(CodeIndex.repo_id == repo_id)

            if file_path:
                # Support pattern matching with %
                if "%" in file_path or "*" in file_path:
                    stmt = stmt.where(CodeIndex.file_path.like(file_path.replace("*", "%")))
                else:
                    stmt = stmt.where(CodeIndex.file_path == file_path)

            if language:
                stmt = stmt.where(CodeIndex.language == language)

            result = await session.execute(stmt.limit(50))
            records = result.scalars().all()

        data = []
        for r in records:
            entry = {
                "file_path": r.file_path,
                "language": r.language,
                "size_bytes": r.size_bytes,
                "line_count": r.line_count,
                "content_hash": r.content_hash,
                "structure": r.structure,
            }

            # If search term provided, filter by content
            if search:
                struct_str = json.dumps(r.structure).lower()
                if search.lower() not in struct_str and search.lower() not in r.file_path.lower():
                    continue

            data.append(entry)

        return ToolResult(success=True, data=data)
    except Exception as e:
        log.warning("code_index_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_query_code_index(repo_id: int) -> Tool:
    return Tool(
        name="query_code_index",
        description="Query the code index database for file structure information. "
                    "Returns parsed structure (imports, classes, functions, routes) for matching files. "
                    "Use this instead of reading files from GitHub — it's faster and free.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Exact file path or pattern with * wildcards (e.g. 'src/api/*' or 'backend/app/models/user.py')",
                },
                "language": {
                    "type": "string",
                    "description": "Filter by language: python, javascript, typescript, json, yaml, etc.",
                },
                "search": {
                    "type": "string",
                    "description": "Search term to filter results (searches file paths and structure content)",
                },
            },
        },
        handler=lambda file_path=None, language=None, search=None: _query_code_index(
            repo_id, file_path, language, search
        ),
    )


# ── Save Issue Record ─────────────────────────────────────────────

async def _save_issue_record(
    repo_id: int,
    github_number: int,
    title: str,
    state: str = "open",
    labels: list[str] | None = None,
    is_milestone_tracker: bool = False,
    linked_issue_numbers: list[int] | None = None,
) -> ToolResult:
    """Save or update an issue record in the local database for tracking."""
    try:
        async with async_session() as session:
            # Check if record exists
            existing = await session.execute(
                select(IssueModel).where(
                    IssueModel.repo_id == repo_id,
                    IssueModel.github_number == github_number,
                )
            )
            record = existing.scalar_one_or_none()

            if record:
                record.title = title
                record.state = state
                record.labels = [{"name": l} for l in (labels or [])]
                record.is_milestone_tracker = is_milestone_tracker
                record.linked_issue_numbers = linked_issue_numbers or []
            else:
                record = IssueModel(
                    repo_id=repo_id,
                    github_number=github_number,
                    title=title,
                    state=state,
                    labels=[{"name": l} for l in (labels or [])],
                    is_milestone_tracker=is_milestone_tracker,
                    linked_issue_numbers=linked_issue_numbers or [],
                )
                session.add(record)

            await session.commit()
            await session.refresh(record)

        return ToolResult(success=True, data={"issue_id": record.id, "github_number": github_number})
    except Exception as e:
        log.warning("code_index_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_save_issue_record(repo_id: int) -> Tool:
    return Tool(
        name="save_issue_record",
        description="Save a created GitHub issue to the local database for tracking. "
                    "Call this AFTER creating each issue with create_issue to keep the DB in sync.",
        parameters={
            "type": "object",
            "properties": {
                "github_number": {"type": "integer", "description": "The issue number returned from create_issue"},
                "title": {"type": "string", "description": "Issue title"},
                "state": {"type": "string", "enum": ["open", "closed"], "description": "Issue state"},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "Label names"},
                "is_milestone_tracker": {"type": "boolean", "description": "True if this is a Milestone Tracker issue"},
                "linked_issue_numbers": {"type": "array", "items": {"type": "integer"}, "description": "Sub-issue numbers for Milestone Trackers"},
            },
            "required": ["github_number", "title"],
        },
        handler=lambda github_number, title, state="open", labels=None, is_milestone_tracker=False, linked_issue_numbers=None: _save_issue_record(
            repo_id, github_number, title, state, labels, is_milestone_tracker, linked_issue_numbers
        ),
    )
