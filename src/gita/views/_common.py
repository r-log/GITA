"""Shared helpers for the view layer."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import Repo


class RepoNotFoundError(LookupError):
    """Raised when a view is asked about a repo that isn't indexed."""


async def resolve_repo(session: AsyncSession, repo_name: str) -> Repo:
    """Return the indexed Repo row for ``repo_name`` or raise.

    Lookup order:
    1. Exact match on ``Repo.name`` (the short CLI name, e.g. "amass").
    2. Case-insensitive match on ``Repo.github_full_name`` (the webhook
       path, e.g. "r-log/AMASS").

    The two-step lookup means CLI callers pay no extra cost (fast path),
    while webhook-triggered jobs get the fallback they need.
    """
    # Fast path — exact short-name match (covers CLI usage).
    stmt = select(Repo).where(Repo.name == repo_name)
    repo = (await session.execute(stmt)).scalar_one_or_none()
    if repo is not None:
        return repo

    # Fallback — case-insensitive match on github_full_name.
    normalized = repo_name.strip().lower()
    stmt2 = select(Repo).where(
        func.lower(Repo.github_full_name) == normalized
    )
    repo = (await session.execute(stmt2)).scalar_one_or_none()
    if repo is not None:
        return repo

    raise RepoNotFoundError(f"repo not indexed: {repo_name!r}")


# ---------------------------------------------------------------------------
# Symbol summary — shared by neighborhood_view and load_bearing_view
# ---------------------------------------------------------------------------
@dataclass
class SymbolBrief:
    """Metadata-only summary of a symbol. No code body, by design."""

    name: str
    kind: str
    line: int
    parent_class: str | None = None


def build_symbol_summary(structure: dict) -> list[SymbolBrief]:
    """Flatten a ``code_index.structure`` JSONB dict into a list of briefs.

    Returned in line order (top-down through the file).
    """
    briefs: list[SymbolBrief] = []
    for cls in structure.get("classes", []):
        briefs.append(
            SymbolBrief(
                name=cls["name"],
                kind=cls["kind"],
                line=cls["start_line"],
            )
        )
    for fn in structure.get("functions", []):
        briefs.append(
            SymbolBrief(
                name=fn["name"],
                kind=fn["kind"],
                line=fn["start_line"],
                parent_class=fn.get("parent_class"),
            )
        )
    briefs.sort(key=lambda b: b.line)
    return briefs
