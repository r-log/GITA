"""Shared helpers for the view layer."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import Repo


class RepoNotFoundError(LookupError):
    """Raised when a view is asked about a repo that isn't indexed."""


async def resolve_repo(session: AsyncSession, repo_name: str) -> Repo:
    """Return the indexed Repo row for ``repo_name`` or raise."""
    stmt = select(Repo).where(Repo.name == repo_name)
    repo = (await session.execute(stmt)).scalar_one_or_none()
    if repo is None:
        raise RepoNotFoundError(f"repo not indexed: {repo_name!r}")
    return repo
