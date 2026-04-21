"""Agent runner functions — shared pipeline for webhook and CLI.

Each runner creates a DB session, LLM client, and GitHub client, runs
the agent recipe, routes the result through the bridge + execute_decision,
and returns a summary dict. The CLI command handlers and the ARQ jobs
both call these runners.

**SHA-based skip for PR reviews:** before running the reviewer, the
runner checks ``agent_actions`` for a prior review of the same PR at
the same head SHA. If found, the review is skipped entirely. This
prevents redundant re-reviews when ``pull_request.synchronize`` events
arrive after the first review already covered that SHA.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.decisions import WriteMode, execute_decision
from gita.agents.pr_reviewer import (
    PRReviewError,
    build_pr_review_decision,
    parse_pr_files,
    run_pr_review,
)
from gita.config import settings
from gita.db.session import SessionLocal
from gita.github.auth import GithubAppAuth
from gita.github.client import GithubClient
from gita.indexer.ingest import index_repository
from gita.llm.client import OpenRouterClient
from gita.views._common import RepoNotFoundError, resolve_repo

logger = logging.getLogger(__name__)

# Evidence tag prefix for SHA-based skip.
_SHA_EVIDENCE_PREFIX = "head_sha:"


# ---------------------------------------------------------------------------
# SHA-based skip (prevents redundant re-reviews)
# ---------------------------------------------------------------------------
async def _check_sha_already_reviewed(
    session: AsyncSession,
    repo_full_name: str,
    head_sha: str,
) -> bool:
    """Return True if a prior review exists for this repo + SHA.

    Looks for an ``agent_actions`` row where:
    - ``repo_name`` matches (case-insensitive)
    - ``agent`` is ``pr_reviewer``
    - ``evidence`` JSONB array contains ``"head_sha:<sha>"``

    Uses Postgres ``@>`` containment operator on the JSONB column.
    """
    repo_lower = repo_full_name.strip().lower()
    sha_tag = f"{_SHA_EVIDENCE_PREFIX}{head_sha}"

    # Use raw SQL for the JSONB containment — asyncpg needs the
    # literal cast on the right-hand side, not a bound parameter.
    # CAST(...) syntax instead of :: to avoid SQLAlchemy's :param parser.
    stmt = text(
        "SELECT id FROM agent_actions "
        "WHERE repo_name = :repo "
        "AND agent = 'pr_reviewer' "
        "AND evidence @> CAST(:sha_json AS JSONB) "
        "LIMIT 1"
    ).bindparams(repo=repo_lower, sha_json=f'["{sha_tag}"]')
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


# ---------------------------------------------------------------------------
# PR review runner
# ---------------------------------------------------------------------------
async def run_pr_review_job(
    repo_full_name: str,
    pr_number: int,
    *,
    head_sha: str | None = None,
) -> dict[str, Any]:
    """Run the full PR review pipeline.

    1. SHA-based skip check
    2. Fetch PR metadata + files from GitHub
    3. Run the PR reviewer recipe (LLM calls)
    4. Build a Decision and route through execute_decision
    5. Return a summary dict

    Raises on misconfiguration (missing API keys). Catches agent errors
    and returns them in the summary.
    """
    owner, repo = repo_full_name.split("/", 1)
    mode = WriteMode(settings.write_mode)

    # --- Early repo resolution (before any external calls) ---
    async with SessionLocal() as session:
        try:
            repo_obj = await resolve_repo(session, repo_full_name)
            repo_name = repo_obj.name  # canonical short name
        except RepoNotFoundError:
            logger.warning(
                "pr_review_no_index repo=%s — skipping", repo_full_name
            )
            return {
                "status": "error",
                "reason": "repo_not_indexed",
                "repo": repo_full_name,
                "pr_number": pr_number,
            }

    # --- Validate required credentials ---
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY not configured")
    if not settings.github_app_id or not settings.github_app_private_key_path:
        raise RuntimeError(
            "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH not configured"
        )

    auth = GithubAppAuth.from_files(
        app_id=settings.github_app_id,
        private_key_path=settings.github_app_private_key_path,
    )

    # --- Fetch PR from GitHub ---
    async with GithubClient(auth=auth) as gh:
        pr_info = await gh.get_pr(owner, repo, pr_number)
        pr_files_json = await gh.get_pr_files(owner, repo, pr_number)

    # Use the live head SHA from the PR (more accurate than the webhook's).
    actual_sha = pr_info.head_sha or head_sha

    # --- SHA-based skip (before LLM calls) ---
    if actual_sha:
        async with SessionLocal() as session:
            already = await _check_sha_already_reviewed(
                session, repo_full_name, actual_sha
            )
        if already:
            logger.info(
                "pr_review_skipped_sha repo=%s pr=%d sha=%s",
                repo_full_name,
                pr_number,
                actual_sha,
            )
            return {
                "status": "skipped",
                "reason": "sha_already_reviewed",
                "repo": repo_full_name,
                "pr_number": pr_number,
                "head_sha": actual_sha,
            }

    # --- Run the PR reviewer ---
    diff_hunks = parse_pr_files(pr_files_json)
    model = settings.ai_default_model

    async with OpenRouterClient(
        api_key=settings.openrouter_api_key, default_model=model
    ) as llm:
        async with SessionLocal() as session:
            try:
                result = await run_pr_review(
                    session, repo_name, pr_info, diff_hunks, llm=llm
                )
            except PRReviewError as exc:
                logger.error(
                    "pr_review_failed repo=%s pr=%d error=%s",
                    repo_full_name,
                    pr_number,
                    exc,
                )
                return {
                    "status": "error",
                    "reason": str(exc),
                    "repo": repo_full_name,
                    "pr_number": pr_number,
                }

    # --- Build decision and execute ---
    decision = build_pr_review_decision(
        result, repo_full_name=repo_full_name, pr_number=pr_number
    )

    # Inject head SHA into evidence for future SHA-based skip lookups.
    if actual_sha:
        decision.evidence.append(f"{_SHA_EVIDENCE_PREFIX}{actual_sha}")

    async with GithubClient(auth=auth) as gh, SessionLocal() as session:
        decision_result = await execute_decision(
            decision,
            mode=mode,
            client=gh if mode != WriteMode.SHADOW else None,
            session=session,
            agent="pr_reviewer",
        )
        await session.commit()

    logger.info(
        "pr_review_complete repo=%s pr=%d outcome=%s verdict=%s sha=%s",
        repo_full_name,
        pr_number,
        decision_result.outcome.value,
        result.verdict,
        actual_sha,
    )

    return {
        "status": "completed",
        "repo": repo_full_name,
        "pr_number": pr_number,
        "head_sha": actual_sha,
        "verdict": result.verdict,
        "findings": len(result.findings),
        "outcome": decision_result.outcome.value,
    }


# ---------------------------------------------------------------------------
# Onboarding runner (webhook-triggered via issues.opened)
# ---------------------------------------------------------------------------
async def run_onboarding_job(
    repo_full_name: str,
    issue_number: int,
) -> dict[str, Any]:
    """Run the onboarding pipeline and post results as a comment.

    Webhook-triggered onboarding always posts as a comment on the
    triggering issue (equivalent to CLI ``--post-to``).
    """
    from gita.agents.onboarding import (
        OnboardingError,
        build_onboarding_comment_decision,
        run_onboarding,
    )

    mode = WriteMode(settings.write_mode)

    # --- Early repo resolution (before any external calls) ---
    async with SessionLocal() as session:
        try:
            repo_obj = await resolve_repo(session, repo_full_name)
            repo_name = repo_obj.name  # canonical short name
        except RepoNotFoundError:
            logger.warning(
                "onboarding_no_index repo=%s — skipping", repo_full_name
            )
            return {
                "status": "error",
                "reason": "repo_not_indexed",
                "repo": repo_full_name,
                "issue_number": issue_number,
            }

    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    model = settings.ai_default_model

    async with OpenRouterClient(
        api_key=settings.openrouter_api_key, default_model=model
    ) as llm:
        async with SessionLocal() as session:
            try:
                result = await run_onboarding(session, repo_name, llm=llm)
            except OnboardingError as exc:
                logger.error(
                    "onboarding_failed repo=%s issue=%d error=%s",
                    repo_full_name,
                    issue_number,
                    exc,
                )
                return {
                    "status": "error",
                    "reason": str(exc),
                    "repo": repo_full_name,
                    "issue_number": issue_number,
                }

    decision = build_onboarding_comment_decision(
        result,
        repo_full_name=repo_full_name,
        issue_number=issue_number,
    )

    if (
        mode != WriteMode.SHADOW
        and settings.github_app_id
        and settings.github_app_private_key_path
    ):
        auth = GithubAppAuth.from_files(
            app_id=settings.github_app_id,
            private_key_path=settings.github_app_private_key_path,
        )
        async with GithubClient(auth=auth) as gh, SessionLocal() as session:
            decision_result = await execute_decision(
                decision,
                mode=mode,
                client=gh,
                session=session,
                agent="onboarding",
            )
            await session.commit()
    else:
        async with SessionLocal() as session:
            decision_result = await execute_decision(
                decision,
                mode=mode,
                session=session,
                agent="onboarding",
            )
            await session.commit()

    logger.info(
        "onboarding_complete repo=%s issue=%d outcome=%s",
        repo_full_name,
        issue_number,
        decision_result.outcome.value,
    )

    return {
        "status": "completed",
        "repo": repo_full_name,
        "issue_number": issue_number,
        "findings": len(result.findings),
        "milestones": len(result.milestones),
        "outcome": decision_result.outcome.value,
    }


# ---------------------------------------------------------------------------
# Git sync helper (fetch + reset to target SHA)
# ---------------------------------------------------------------------------
_GIT_TIMEOUT = 120  # seconds


def _git_sync(root_path: Path, after_sha: str | None) -> tuple[bool, str]:
    """Fetch from origin and reset the working tree to a target SHA.

    Returns ``(success, error_message)`` — error_message is empty on success.
    Safe to call with ``max_jobs=1`` (no concurrent access to the worktree).
    """
    try:
        subprocess.run(
            ["git", "-C", str(root_path), "fetch", "origin"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return False, f"git fetch failed: {exc}"

    target = after_sha or "origin/HEAD"
    try:
        subprocess.run(
            ["git", "-C", str(root_path), "reset", "--hard", target],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return False, f"git reset failed: {exc}"

    return True, ""


# ---------------------------------------------------------------------------
# Reindex runner (webhook-triggered via push events)
# ---------------------------------------------------------------------------
async def run_reindex_job(
    repo_full_name: str,
    after_sha: str | None = None,
) -> dict[str, Any]:
    """Fetch latest code and incrementally re-index.

    1. Resolve the repo by github_full_name (Day 1 fallback)
    2. ``git fetch origin`` + ``git reset --hard <sha>``
    3. Call ``index_repository()`` (incremental when possible)
    4. Return a summary dict
    """
    # --- Resolve repo early (before git operations) ---
    async with SessionLocal() as session:
        try:
            repo = await resolve_repo(session, repo_full_name)
            repo_name = repo.name
            root_path = Path(repo.root_path)
        except RepoNotFoundError:
            logger.warning(
                "reindex_no_index repo=%s — skipping", repo_full_name
            )
            return {
                "status": "error",
                "reason": "repo_not_indexed",
                "repo": repo_full_name,
                "after_sha": after_sha,
            }

    if not root_path.is_dir():
        logger.error(
            "reindex_root_missing repo=%s path=%s", repo_full_name, root_path
        )
        return {
            "status": "error",
            "reason": "root_path_missing",
            "repo": repo_full_name,
            "after_sha": after_sha,
        }

    # --- Git sync ---
    ok, err = _git_sync(root_path, after_sha)
    if not ok:
        logger.error(
            "reindex_git_sync_failed repo=%s error=%s", repo_full_name, err
        )
        return {
            "status": "error",
            "reason": "git_sync_failed",
            "repo": repo_full_name,
            "after_sha": after_sha,
            "detail": err,
        }

    # --- Re-index ---
    async with SessionLocal() as session:
        result = await index_repository(
            session, repo_name, root_path, github_full_name=repo_full_name
        )
        await session.commit()

    logger.info(
        "reindex_complete repo=%s mode=%s files=%d after_sha=%s",
        repo_full_name,
        result.mode,
        result.files_indexed,
        after_sha,
    )

    return {
        "status": "completed",
        "repo": repo_full_name,
        "after_sha": after_sha,
        "mode": result.mode,
        "files_indexed": result.files_indexed,
        "files_deleted": result.files_deleted,
        "edges_total": result.edges_total,
    }
