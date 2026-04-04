"""
Push-triggered incremental context updater.

On every push event, extracts changed files from commits, re-parses only those files,
and updates the code index in the database. Fully deterministic -- zero LLM cost.
Bypasses the Supervisor -- this is a deterministic trigger, not an event classification.
"""

import structlog

from src.core.repo_manager import upsert_repository
from src.indexer.indexer import reindex_files

log = structlog.get_logger()


def _extract_changed_files(payload: dict) -> tuple[set[str], set[str]]:
    """Extract changed and removed files from push payload commits."""
    changed = set()
    removed = set()
    for commit in payload.get("commits", []):
        changed.update(commit.get("added", []))
        changed.update(commit.get("modified", []))
        removed.update(commit.get("removed", []))
    # Don't try to read files that were deleted
    changed -= removed
    return changed, removed


async def update_context_on_push(
    repo_id: int,
    repo_full_name: str,
    installation_id: int,
    payload: dict,
) -> dict:
    """
    Incrementally update the code index based on pushed changes.
    Downloads only changed files, re-parses them deterministically, updates DB.
    Zero LLM cost.
    """
    log.info("context_update_start", repo=repo_full_name)

    # 1. Extract changed files from commits
    changed_files, removed_files = _extract_changed_files(payload)
    if not changed_files and not removed_files:
        log.info("context_update_skip", reason="no_file_changes")
        return {"status": "skipped", "reason": "no_file_changes"}

    log.info("context_update_files", changed=len(changed_files), removed=len(removed_files))

    # 2. Reindex only the changed/removed files (deterministic, zero LLM cost)
    result = await reindex_files(
        installation_id=installation_id,
        repo_full_name=repo_full_name,
        repo_id=repo_id,
        changed_files=changed_files,
        removed_files=removed_files,
    )

    log.info(
        "context_update_complete",
        repo=repo_full_name,
        files_updated=result["files_updated"],
        files_removed=result["files_removed"],
    )

    return {
        "status": "success",
        "files_changed": len(changed_files),
        "files_removed": len(removed_files),
        "files_updated": result["files_updated"],
    }


async def process_context_update(ctx, repo_full_name: str, installation_id: int, payload: dict):
    """ARQ task wrapper for push context updates."""
    try:
        # Resolve repo_id from DB
        repo_github_id = payload.get("repository", {}).get("id", 0)
        if not repo_github_id:
            log.warning("context_update_no_repo_id")
            return

        repo_id = await upsert_repository(repo_github_id, repo_full_name, installation_id)
        await update_context_on_push(repo_id, repo_full_name, installation_id, payload)
    except Exception as e:
        log.error("context_update_error", repo=repo_full_name, error=str(e))
