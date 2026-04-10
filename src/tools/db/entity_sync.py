"""
Entity sync — persist webhook data into the database for RAG.

All functions are fire-and-forget with try/except guards so they never
block the main webhook/worker flow. Each function upserts to handle
duplicate deliveries gracefully.
"""

from datetime import datetime

import structlog
from sqlalchemy import select

from src.core.database import async_session
from src.models.event import EventModel
from src.models.commit import CommitModel
from src.models.comment import CommentModel
from src.models.review import ReviewModel
from src.models.diff import DiffModel
from src.models.issue import IssueModel
from src.models.pull_request import PullRequestModel

log = structlog.get_logger()


def _parse_dt(s: str | None) -> datetime | None:
    """Parse GitHub ISO 8601 timestamp, return naive UTC datetime for Postgres."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        # Strip timezone info — store as naive UTC (Postgres TIMESTAMP WITHOUT TIME ZONE)
        if dt.tzinfo is not None:
            from datetime import timezone
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


# ── Events ────────────────────────────────────────────────────────


async def persist_event(
    repo_id: int,
    delivery_id: str,
    event_type: str,
    action: str | None,
    sender_login: str | None,
    target_type: str | None,
    target_number: int | None,
    payload: dict,
) -> None:
    """Store a raw webhook event. Skips silently on duplicate delivery_id."""
    try:
        async with async_session() as session:
            existing = await session.execute(
                select(EventModel.id).where(EventModel.delivery_id == delivery_id)
            )
            if existing.scalar_one_or_none():
                return

            session.add(EventModel(
                repo_id=repo_id,
                delivery_id=delivery_id,
                event_type=event_type,
                action=action,
                sender_login=sender_login,
                target_type=target_type,
                target_number=target_number,
                payload=payload,
            ))
            await session.commit()
    except Exception as e:
        log.warning("persist_event_failed", delivery_id=delivery_id, error=str(e))


# ── Issues ────────────────────────────────────────────────────────


async def persist_issue_from_payload(repo_id: int, issue_data: dict) -> None:
    """Upsert an issue record from a GitHub webhook issue object."""
    number = issue_data.get("number")
    if not number:
        return
    try:
        async with async_session() as session:
            existing = await session.execute(
                select(IssueModel).where(
                    IssueModel.repo_id == repo_id,
                    IssueModel.github_number == number,
                )
            )
            record = existing.scalar_one_or_none()

            title = (issue_data.get("title") or "")[:255]
            state = issue_data.get("state", "open")
            body = issue_data.get("body")
            author = (issue_data.get("user") or {}).get("login")
            labels = [{"name": l["name"]} for l in issue_data.get("labels", [])]
            assignees = [a["login"] for a in issue_data.get("assignees", [])]
            gh_created = _parse_dt(issue_data.get("created_at"))
            closed = _parse_dt(issue_data.get("closed_at"))

            if record:
                record.title = title
                record.state = state
                record.body = body
                record.author = author
                record.labels = labels
                record.assignees = assignees
                record.github_created_at = gh_created
                record.closed_at = closed
            else:
                session.add(IssueModel(
                    repo_id=repo_id,
                    github_number=number,
                    title=title,
                    state=state,
                    body=body,
                    author=author,
                    labels=labels,
                    assignees=assignees,
                    github_created_at=gh_created,
                    closed_at=closed,
                ))
            await session.commit()
    except Exception as e:
        log.warning("persist_issue_failed", number=number, error=str(e))


# ── Pull Requests ─────────────────────────────────────────────────


async def persist_pr_from_payload(repo_id: int, pr_data: dict) -> None:
    """Upsert a pull request record from a GitHub webhook PR object."""
    number = pr_data.get("number")
    if not number:
        return
    try:
        async with async_session() as session:
            existing = await session.execute(
                select(PullRequestModel).where(
                    PullRequestModel.repo_id == repo_id,
                    PullRequestModel.github_number == number,
                )
            )
            record = existing.scalar_one_or_none()

            title = (pr_data.get("title") or "")[:255]
            state = pr_data.get("state", "open")
            body = pr_data.get("body")
            author = (pr_data.get("user") or {}).get("login")
            base_branch = (pr_data.get("base") or {}).get("ref")
            head_branch = (pr_data.get("head") or {}).get("ref")
            gh_created = _parse_dt(pr_data.get("created_at"))
            merged = _parse_dt(pr_data.get("merged_at"))
            merged_by_user = pr_data.get("merged_by")
            merged_by = merged_by_user["login"] if isinstance(merged_by_user, dict) else None
            commit_count = pr_data.get("commits")

            if record:
                record.title = title
                record.state = state
                record.body = body
                record.author = author
                record.base_branch = base_branch
                record.head_branch = head_branch
                record.github_created_at = gh_created
                record.merged_at = merged
                record.merged_by = merged_by
                record.commit_count = commit_count
            else:
                session.add(PullRequestModel(
                    repo_id=repo_id,
                    github_number=number,
                    title=title,
                    state=state,
                    body=body,
                    author=author,
                    base_branch=base_branch,
                    head_branch=head_branch,
                    github_created_at=gh_created,
                    merged_at=merged,
                    merged_by=merged_by,
                    commit_count=commit_count,
                ))
            await session.commit()
    except Exception as e:
        log.warning("persist_pr_failed", number=number, error=str(e))


# ── Comments ──────────────────────────────────────────────────────


async def persist_comment(
    repo_id: int,
    comment_data: dict,
    target_type: str,
    target_number: int,
) -> None:
    """Upsert a single comment from a GitHub comment object."""
    github_id = comment_data.get("id")
    if not github_id:
        return
    try:
        async with async_session() as session:
            existing = await session.execute(
                select(CommentModel).where(CommentModel.github_id == github_id)
            )
            record = existing.scalar_one_or_none()

            user = comment_data.get("user") or {}
            author = user.get("login")
            is_bot = user.get("type") == "Bot" or (author or "").endswith("[bot]")
            body = comment_data.get("body")
            gh_created = _parse_dt(comment_data.get("created_at"))
            gh_updated = _parse_dt(comment_data.get("updated_at"))

            if record:
                record.body = body
                record.github_updated_at = gh_updated
            else:
                session.add(CommentModel(
                    repo_id=repo_id,
                    github_id=github_id,
                    target_type=target_type,
                    target_number=target_number,
                    author_login=author,
                    body=body,
                    is_bot=is_bot,
                    github_created_at=gh_created,
                    github_updated_at=gh_updated,
                ))
            await session.commit()
    except Exception as e:
        log.warning("persist_comment_failed", github_id=github_id, error=str(e))


async def persist_comments_batch(
    repo_id: int,
    comments: list[dict],
    target_type: str,
    target_number: int,
) -> None:
    """Persist multiple comments (e.g. when fetched for dedup check)."""
    for comment_data in comments:
        await persist_comment(repo_id, comment_data, target_type, target_number)


# ── Reviews ───────────────────────────────────────────────────────


async def persist_reviews(repo_id: int, pr_number: int, reviews: list[dict]) -> None:
    """Persist PR reviews from GitHub API response."""
    try:
        async with async_session() as session:
            for review_data in reviews:
                github_id = review_data.get("id")
                if not github_id:
                    continue

                existing = await session.execute(
                    select(ReviewModel).where(ReviewModel.github_id == github_id)
                )
                if existing.scalar_one_or_none():
                    continue

                user = review_data.get("user") or {}
                session.add(ReviewModel(
                    repo_id=repo_id,
                    pr_number=pr_number,
                    github_id=github_id,
                    author_login=user.get("login"),
                    state=review_data.get("state", "commented"),
                    body=review_data.get("body"),
                    submitted_at=_parse_dt(review_data.get("submitted_at")),
                ))
            await session.commit()
    except Exception as e:
        log.warning("persist_reviews_failed", pr=pr_number, error=str(e))


# ── Diffs ─────────────────────────────────────────────────────────


async def persist_diff(repo_id: int, pr_number: int, head_sha: str, diff_text: str, diff_size: int) -> None:
    """Cache a PR diff keyed by head SHA."""
    try:
        async with async_session() as session:
            existing = await session.execute(
                select(DiffModel.id).where(
                    DiffModel.repo_id == repo_id,
                    DiffModel.pr_number == pr_number,
                    DiffModel.head_sha == head_sha,
                )
            )
            if existing.scalar_one_or_none():
                return

            session.add(DiffModel(
                repo_id=repo_id,
                pr_number=pr_number,
                head_sha=head_sha,
                diff_text=diff_text,
                diff_size=diff_size,
            ))
            await session.commit()
    except Exception as e:
        log.warning("persist_diff_failed", pr=pr_number, error=str(e))


async def get_cached_diff(repo_id: int, pr_number: int, head_sha: str) -> str | None:
    """Retrieve a cached diff by PR number and head SHA. Returns None on miss."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(DiffModel.diff_text).where(
                    DiffModel.repo_id == repo_id,
                    DiffModel.pr_number == pr_number,
                    DiffModel.head_sha == head_sha,
                )
            )
            return result.scalar_one_or_none()
    except Exception:
        return None


# ── Commits ───────────────────────────────────────────────────────


async def persist_commits(repo_id: int, commits_data: list[dict]) -> int:
    """Persist commit records from a push payload. Returns count of new commits stored."""
    stored = 0
    try:
        async with async_session() as session:
            for c in commits_data:
                sha = c.get("id", "")
                if not sha:
                    continue

                existing = await session.execute(
                    select(CommitModel.id).where(
                        CommitModel.repo_id == repo_id,
                        CommitModel.sha == sha,
                    )
                )
                if existing.scalar_one_or_none():
                    continue

                author = c.get("author") or {}
                session.add(CommitModel(
                    repo_id=repo_id,
                    sha=sha,
                    message=c.get("message", ""),
                    author_name=author.get("name"),
                    author_email=author.get("email"),
                    author_login=author.get("username"),
                    committed_at=_parse_dt(c.get("timestamp")),
                    files_added=c.get("added", []),
                    files_modified=c.get("modified", []),
                    files_removed=c.get("removed", []),
                ))
                stored += 1
            await session.commit()
    except Exception as e:
        log.warning("persist_commits_failed", repo_id=repo_id, error=str(e))
    return stored
