"""Bring a repository's history memory up to date.

``sync_history`` reads the merges since the last sync (all of them the
first time), stores each PR with its files and commits, and, for every fix
PR, the PRs whose lines it changed. It is incremental and idempotent: a
second run with nothing new merged does nothing, and if the default
branch's history was rewritten it starts over.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import DefectLink, HistoryPR, HistoryPRCommit, HistoryPRFile, Repo
from gita.history.git_history import GitError, MergedPR, git, is_shallow, link_fix, list_merged_prs
from gita.indexer.ingest import _get_or_create_repo

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    repo_name: str
    head_sha: str
    mode: str  # "full" | "incremental" | "noop"
    prs_added: int
    fixes_linked: int
    links_added: int
    shallow: bool
    elapsed_s: float


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    try:
        git(root, "merge-base", "--is-ancestor", older, newer)
    except GitError:
        return False
    return True


async def _clear_history(session: AsyncSession, repo_id) -> None:
    for model in (DefectLink, HistoryPRCommit, HistoryPRFile, HistoryPR):
        await session.execute(delete(model).where(model.repo_id == repo_id))


async def sync_history(
    session: AsyncSession, repo_name: str, root_path: Path, *, full: bool = False
) -> SyncResult:
    """Read new merges from ``root_path`` (a clone with history) into the memory tables."""
    start = time.monotonic()
    root = root_path.resolve()
    repo: Repo = await _get_or_create_repo(session, repo_name, root)
    head = (await asyncio.to_thread(git, root, "rev-parse", "HEAD")).strip()
    shallow = await asyncio.to_thread(is_shallow, root)
    if shallow:
        logger.warning("history_sync_shallow repo=%s: history before the cut is missing", repo_name)

    since = None if full else repo.history_synced_sha
    if since and not await asyncio.to_thread(_is_ancestor, root, since, head):
        logger.warning("history_sync_rewritten repo=%s: starting over", repo_name)
        since = None
    if since == head:
        return SyncResult(
            repo_name, head, "noop", 0, 0, 0, shallow, round(time.monotonic() - start, 2)
        )
    if since is None:
        await _clear_history(session, repo.id)

    found: list[MergedPR] = await asyncio.to_thread(list_merged_prs, root, since)
    known = set(
        (await session.execute(select(HistoryPR.number).where(HistoryPR.repo_id == repo.id)))
        .scalars()
        .all()
    )
    new = [pr for pr in found if pr.number not in known]
    for pr in new:
        session.add(
            HistoryPR(
                repo_id=repo.id,
                number=pr.number,
                title=pr.title,
                branch=pr.branch,
                kind=pr.kind,
                merge_sha=pr.merge_sha,
                base_sha=pr.base_sha,
                head_sha=pr.head_sha,
                merged_at=pr.merged_at,
                author=pr.author,
                files_changed=len(pr.files),
                additions=sum(f.additions for f in pr.files),
                deletions=sum(f.deletions for f in pr.files),
            )
        )
        session.add_all(
            HistoryPRFile(
                repo_id=repo.id,
                pr_number=pr.number,
                path=f.path,
                old_path=f.old_path,
                additions=f.additions,
                deletions=f.deletions,
            )
            for f in pr.files
        )
        if pr.commits:
            # A commit belongs to the first PR that merged it (oldest first).
            await session.execute(
                insert(HistoryPRCommit)
                .values(
                    [
                        {"repo_id": repo.id, "commit_sha": c, "pr_number": pr.number}
                        for c in pr.commits
                    ]
                )
                .on_conflict_do_nothing()
            )
    await session.flush()

    # Link each new fix to the PRs whose lines it changed. The commit and
    # head maps are read once; git work runs off the event loop.
    fixes = [pr for pr in new if pr.kind == "fix"]
    links_added = 0
    if fixes:
        owner = dict(
            (
                await session.execute(
                    select(HistoryPRCommit.commit_sha, HistoryPRCommit.pr_number).where(
                        HistoryPRCommit.repo_id == repo.id
                    )
                )
            ).all()
        )
        heads = dict(
            (
                await session.execute(
                    select(HistoryPR.number, HistoryPR.head_sha).where(HistoryPR.repo_id == repo.id)
                )
            ).all()
        )

        def owner_of(commits):
            return {c: owner[c] for c in commits if c in owner}

        for fix in fixes:
            links = await asyncio.to_thread(link_fix, root, fix, owner_of, heads.get)
            session.add_all(
                DefectLink(
                    repo_id=repo.id,
                    fix_pr=link.fix_pr,
                    introducing_pr=link.introducing_pr,
                    share=link.share,
                    lines=link.lines,
                )
                for link in links
            )
            links_added += len(links)

    repo.history_synced_sha = head
    repo.history_synced_at = datetime.now(UTC)
    await session.flush()
    result = SyncResult(
        repo_name=repo_name,
        head_sha=head,
        mode="full" if since is None else "incremental",
        prs_added=len(new),
        fixes_linked=len(fixes),
        links_added=links_added,
        shallow=shallow,
        elapsed_s=round(time.monotonic() - start, 2),
    )
    logger.info(
        "history_sync repo=%s mode=%s prs=%d fixes=%d links=%d %.1fs",
        repo_name,
        result.mode,
        result.prs_added,
        result.fixes_linked,
        result.links_added,
        result.elapsed_s,
    )
    return result
