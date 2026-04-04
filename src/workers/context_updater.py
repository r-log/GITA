"""
Push-triggered incremental context updater.

On every push event, extracts changed files from commits, reads only those files,
and updates the stored project snapshot with a single cheap LLM call.
Bypasses the Supervisor — this is a deterministic trigger, not an event classification.
"""

import asyncio
import json
import re
from datetime import datetime

import structlog
from openai import AsyncOpenAI
from sqlalchemy import select

from src.core.config import settings
from src.core.database import async_session
from src.core.repo_manager import upsert_repository
from src.models.onboarding_run import OnboardingRun
from src.tools.github.repos import _read_file

log = structlog.get_logger()

_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=settings.openrouter_api_key,
    timeout=60.0,
)

# Max files to read per push (cost control)
MAX_FILES_PER_PUSH = 10
# Max chars per file content in LLM context
MAX_CHARS_PER_FILE = 3000
# Total LLM context budget for file contents
MAX_TOTAL_CHARS = 30000


async def _load_latest_snapshot(repo_id: int) -> OnboardingRun | None:
    """Load the latest successful onboarding run for a repo."""
    async with async_session() as session:
        stmt = (
            select(OnboardingRun)
            .where(
                OnboardingRun.repo_id == repo_id,
                OnboardingRun.status.in_(["success", "partial", "context_update"]),
            )
            .order_by(OnboardingRun.completed_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


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


def _extract_json(text: str) -> str:
    """Extract JSON from LLM response."""
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return text
    fence_match = re.search(r"```(?:json)?\s*\n(\{[\s\S]*?\})\s*```", text)
    if fence_match:
        return fence_match.group(1).strip()
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        return text[first_brace:last_brace + 1]
    return text


async def update_context_on_push(
    repo_id: int,
    repo_full_name: str,
    installation_id: int,
    payload: dict,
) -> dict:
    """
    Incrementally update the stored project snapshot based on pushed changes.
    Reads only changed files, makes one cheap LLM call to update summaries.
    """
    log.info("context_update_start", repo=repo_full_name)

    # 1. Extract changed files from commits
    changed_files, removed_files = _extract_changed_files(payload)
    if not changed_files and not removed_files:
        log.info("context_update_skip", reason="no_file_changes")
        return {"status": "skipped", "reason": "no_file_changes"}

    log.info("context_update_files", changed=len(changed_files), removed=len(removed_files))

    # 2. Load latest snapshot
    latest_run = await _load_latest_snapshot(repo_id)
    if not latest_run:
        log.info("context_update_skip", reason="no_prior_onboarding")
        return {"status": "skipped", "reason": "no_prior_onboarding"}

    snapshot = latest_run.repo_snapshot or {}
    deep_dive = snapshot.get("deep_dive", {})
    existing_summaries = deep_dive.get("file_summaries", {})

    # 3. Check if any changed files are relevant (tracked in snapshot)
    relevant_changed = changed_files & set(existing_summaries.keys())
    new_files = changed_files - set(existing_summaries.keys())

    # If no overlap and few new files, still process (new files could be important)
    if not relevant_changed and len(new_files) == 0 and not removed_files:
        log.info("context_update_skip", reason="no_relevant_changes")
        return {"status": "skipped", "reason": "no_relevant_changes"}

    # 4. Read changed files from GitHub (parallel, capped)
    files_to_read = list(changed_files)[:MAX_FILES_PER_PUSH]
    read_tasks = [
        _read_file(installation_id, repo_full_name, path)
        for path in files_to_read
    ]
    results = await asyncio.gather(*read_tasks, return_exceptions=True)

    file_contents = {}
    total_chars = 0
    for path, result in zip(files_to_read, results):
        if isinstance(result, Exception) or not result.success:
            continue
        content = result.data.get("content", "")
        if total_chars + len(content) > MAX_TOTAL_CHARS:
            content = content[:MAX_CHARS_PER_FILE] + "\n... [truncated]"
        file_contents[path] = content[:MAX_CHARS_PER_FILE]
        total_chars += len(file_contents[path])

    log.info("context_update_files_read", count=len(file_contents))

    # 5. Build LLM prompt
    prompt_parts = [
        "You are updating a project snapshot after a code push. Below are the current file summaries and the new file contents.\n",
        "## Current File Summaries\n",
        json.dumps(existing_summaries, indent=2)[:5000],
        "\n\n## Current Features Found\n",
        json.dumps(deep_dive.get("features_found", []), indent=2)[:3000],
        "\n\n## Changed Files (new content)\n",
    ]

    for path, content in file_contents.items():
        prompt_parts.append(f"\n### {path}\n```\n{content}\n```\n")

    if removed_files:
        prompt_parts.append(f"\n## Removed Files\n{json.dumps(list(removed_files))}\n")

    prompt_parts.append("""
## Instructions
Update the file_summaries for changed files. Add entries for new files. Remove entries for deleted files.
If changes affect the features_found or gaps_found lists, update those too.
Only return the CHANGED sections — don't repeat unchanged entries.

Respond with JSON:
{
  "updated_file_summaries": {"path": {"purpose": "...", "status": "complete|partial|stub", "key_elements": [...]}},
  "removed_files": ["paths to remove from summaries"],
  "features_updated": [{"name": "...", "status": "...", "evidence": "..."}],
  "gaps_updated": [{"area": "...", "severity": "...", "details": "..."}],
  "features_changed": false,
  "gaps_changed": false
}
""")

    user_content = "".join(prompt_parts)

    # 6. Single Haiku LLM call
    try:
        response = await _client.chat.completions.create(
            model=settings.ai_model_context_updater,
            messages=[
                {"role": "system", "content": "You are a code analysis assistant updating a project snapshot incrementally."},
                {"role": "user", "content": user_content},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = _extract_json(response.choices[0].message.content or "")
        updates = json.loads(raw)
        # Capture token usage
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "llm_calls": 1,
            }
    except Exception as e:
        log.error("context_update_llm_failed", error=str(e))
        return {"status": "failed", "error": str(e)}

    # 7. Merge updates into snapshot
    updated_snapshot = dict(snapshot)
    updated_deep_dive = dict(deep_dive)

    # Update file summaries
    merged_summaries = dict(existing_summaries)
    for path, summary in updates.get("updated_file_summaries", {}).items():
        merged_summaries[path] = summary
    for path in updates.get("removed_files", []):
        merged_summaries.pop(path, None)
    # Also remove files we know were deleted
    for path in removed_files:
        merged_summaries.pop(path, None)
    updated_deep_dive["file_summaries"] = merged_summaries

    # Update features/gaps only if LLM says they changed
    if updates.get("features_changed"):
        updated_deep_dive["features_found"] = updates.get("features_updated", deep_dive.get("features_found", []))
    if updates.get("gaps_changed"):
        updated_deep_dive["gaps_found"] = updates.get("gaps_updated", deep_dive.get("gaps_found", []))

    updated_snapshot["deep_dive"] = updated_deep_dive

    # 8. Save as new OnboardingRun
    from src.tools.db.onboarding import _save_onboarding_run
    await _save_onboarding_run(
        repo_id=repo_id,
        status="context_update",
        repo_snapshot=updated_snapshot,
        suggested_plan=latest_run.suggested_plan or {},
        existing_state=latest_run.existing_state or {},
        actions_taken=[{
            "type": "context_update",
            "files_changed": list(changed_files),
            "files_removed": list(removed_files),
            "files_read": list(file_contents.keys()),
            "usage": usage,
        }],
        confidence=latest_run.confidence or 0.0,
    )

    log.info(
        "context_update_complete",
        repo=repo_full_name,
        files_updated=len(updates.get("updated_file_summaries", {})),
        files_removed=len(removed_files),
    )

    return {
        "status": "success",
        "files_changed": len(changed_files),
        "files_removed": len(removed_files),
        "summaries_updated": len(updates.get("updated_file_summaries", {})),
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
