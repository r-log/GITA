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

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.decisions import (
    Decision,
    Outcome,
    WriteMode,
    execute_decision,
)
from gita.agents.pr_reviewer import (
    PRReviewError,
    build_pr_review_decision,
    parse_pr_files,
    run_pr_review,
)
from gita.agents.test_generator import (
    TestGenerationArtifact,
    build_test_generation_decisions,
    has_existing_tests,
    is_feasible,
    run_test_generation,
)
from gita.config import settings
from gita.db.session import SessionLocal
from gita.github.auth import GithubAppAuth
from gita.github.client import GithubClient
from gita.indexer.embeddings import make_embedding_client
from gita.indexer.ingest import IngestResult, index_repository
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
    *,
    redis: Any = None,
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
            repo_id = repo.id
            repo_name = repo.name
            root_path = Path(repo.root_path)
            repo_auto_test_gen = bool(repo.auto_test_generation)
            repo_default_branch = repo.default_branch or "main"
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
    embedding_client = make_embedding_client()
    try:
        async with SessionLocal() as session:
            result = await index_repository(
                session,
                repo_name,
                root_path,
                github_full_name=repo_full_name,
                embedding_client=embedding_client,
            )
            await session.commit()
    finally:
        if embedding_client is not None:
            await embedding_client.close()

    logger.info(
        "reindex_complete repo=%s mode=%s files=%d after_sha=%s",
        repo_full_name,
        result.mode,
        result.files_indexed,
        after_sha,
    )

    # Post-reindex auto-test-generation trigger (Week 9). Best-effort:
    # the reindex job's "completed" status does not depend on whether
    # any test_gen jobs were enqueued.
    test_gen_summary: dict[str, Any] = {}
    try:
        test_gen_summary = await _maybe_enqueue_test_gen_jobs(
            repo_full_name=repo_full_name,
            repo_id=repo_id,
            repo_auto_test_gen=repo_auto_test_gen,
            repo_default_branch=repo_default_branch,
            root_path=root_path,
            ingest_result=result,
            redis=redis,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "test_gen_trigger_failed repo=%s error=%s",
            repo_full_name,
            exc,
        )
        test_gen_summary = {"status": "trigger_error", "error": str(exc)}

    return {
        "status": "completed",
        "repo": repo_full_name,
        "after_sha": after_sha,
        "mode": result.mode,
        "files_indexed": result.files_indexed,
        "files_deleted": result.files_deleted,
        "files_embedded": result.files_embedded,
        "edges_total": result.edges_total,
        "added_files": list(result.added_files),
        "test_gen_trigger": test_gen_summary,
    }


# ---------------------------------------------------------------------------
# Post-reindex auto-trigger for test generation (Week 9)
# ---------------------------------------------------------------------------
async def _maybe_enqueue_test_gen_jobs(
    *,
    repo_full_name: str,
    repo_id: Any,
    repo_auto_test_gen: bool,
    repo_default_branch: str,
    root_path: Path,
    ingest_result: IngestResult,
    redis: Any,
) -> dict[str, Any]:
    """Apply Stages A → B → cap and enqueue ``generate_tests`` jobs.

    Returns a structured summary that includes counts at every gate so
    the reindex log makes it obvious why nothing fired (which is the
    common case under default config).

    No-ops in any of the following cases — each one is intentional:

    * ``redis`` is None (called from CLI, which never auto-triggers)
    * ``AUTO_TEST_GEN_ENABLED`` env flag is false (global kill switch)
    * ``Repo.auto_test_generation`` is false (per-repo opt-in off)
    * ingest mode wasn't "incremental" (full reindex is too noisy)
    * ``ingest_result.added_files`` is empty
    """
    summary: dict[str, Any] = {
        "status": "skipped",
        "reason": None,
        "added_files": list(ingest_result.added_files),
        "after_stage_a": [],
        "after_stage_b": [],
        "enqueued": [],
    }

    if redis is None:
        summary["reason"] = "no_redis_pool"
        return summary
    if not settings.auto_test_gen_enabled:
        summary["reason"] = "global_kill_switch_off"
        return summary
    if not repo_auto_test_gen:
        summary["reason"] = "repo_opt_in_off"
        return summary
    if ingest_result.mode != "incremental":
        summary["reason"] = f"mode={ingest_result.mode}"
        return summary
    if not ingest_result.added_files:
        summary["reason"] = "no_added_files"
        return summary

    # --- Stage A: filesystem-based test existence ---
    after_stage_a: list[str] = []
    for path in ingest_result.added_files:
        result_a = has_existing_tests(root_path, path)
        if result_a.proceed:
            after_stage_a.append(path)
        else:
            logger.info(
                "test_gen_stage_a_skip repo=%s file=%s reason=%s",
                repo_full_name,
                path,
                result_a.reason,
            )
    summary["after_stage_a"] = after_stage_a

    # --- Stage B: feasibility (DB + structure) ---
    after_stage_b: list[str] = []
    base_sha = ingest_result.head_sha or "unknown"
    async with SessionLocal() as session:
        for path in after_stage_a:
            result_b = await is_feasible(
                session, repo_id, repo_full_name, path
            )
            if result_b.proceed:
                after_stage_b.append(path)
            else:
                logger.info(
                    "test_gen_stage_b_skip repo=%s file=%s reason=%s",
                    repo_full_name,
                    path,
                    result_b.reason,
                )
    summary["after_stage_b"] = after_stage_b

    if not after_stage_b:
        summary["status"] = "no_candidates"
        return summary

    # --- Stage C: per-reindex cap + deterministic ordering ---
    after_stage_b.sort()  # alphabetical — same retried push picks same file
    cap = max(0, settings.auto_test_gen_max_per_reindex)
    selected = after_stage_b[:cap]

    enqueued: list[dict[str, str]] = []
    repo_lower = repo_full_name.strip().lower()
    sha7 = base_sha[:7]
    for path in selected:
        job_id = f"generate-tests:{repo_lower}:{path}:{sha7}"
        try:
            arq_job = await redis.enqueue_job(
                "generate_tests",
                _job_id=job_id,
                repo_full_name=repo_full_name,
                target_file=path,
                target_repo=repo_full_name,
                base_branch=repo_default_branch,
                base_sha=base_sha,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "test_gen_enqueue_failed repo=%s file=%s error=%s",
                repo_full_name,
                path,
                exc,
            )
            continue
        if arq_job is None:
            # ARQ-level dedupe: a job with this _job_id is already
            # queued or running — perfectly fine, count as deduped.
            logger.info(
                "test_gen_enqueue_deduped job_id=%s", job_id
            )
            enqueued.append({"target_file": path, "job_id": job_id, "deduped": True})
            continue
        logger.info(
            "test_gen_enqueued job_id=%s file=%s sha=%s",
            job_id,
            path,
            sha7,
        )
        enqueued.append({"target_file": path, "job_id": job_id, "deduped": False})

    summary["status"] = "enqueued" if enqueued else "no_candidates"
    summary["enqueued"] = enqueued
    summary["cap"] = cap
    summary["dropped_over_cap"] = max(0, len(after_stage_b) - cap)
    return summary


# ---------------------------------------------------------------------------
# Test-generation runner (Week 9)
# ---------------------------------------------------------------------------
_PROGRESS_OUTCOMES = {
    Outcome.EXECUTED,
    Outcome.SHADOW_LOGGED,
    Outcome.DEDUPED,
}


async def run_test_generation_job(
    repo_full_name: str,
    target_file: str,
    *,
    target_repo: str | None = None,
    base_branch: str | None = None,
    base_sha: str | None = None,
    fallback_issue: int | None = None,
    test_file_path: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Generate a verified pytest test file and (optionally) push it.

    Behaviour mirrors the CLI ``cmd_generate_tests`` flow but returns a
    structured summary instead of printing. Used by the CLI handler
    (which formats the dict for humans) and by the post-reindex
    auto-trigger ARQ job.

    ``repo_full_name`` is the indexed repo identifier — short CLI name
    or GitHub ``owner/repo``; resolves both ways via ``resolve_repo``.

    ``target_repo`` (when not ``None``) is the GitHub ``owner/repo``
    that branch + file write + PR all target. ``None`` means
    local-only (recipe + verification, no GitHub side-effects).

    Raises on misconfiguration (missing API keys); returns
    ``{"status": "error", "reason": ...}`` for runtime failures so
    callers don't need their own try/except.
    """
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    need_github = target_repo is not None
    if need_github and (
        not settings.github_app_id
        or not settings.github_app_private_key_path
    ):
        raise RuntimeError(
            "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH must be set "
            "for the push flow"
        )

    mode = WriteMode(settings.write_mode)
    resolved_model = model or settings.ai_default_model

    # --- Resolve indexed repo + repo_root ---
    async with SessionLocal() as session:
        try:
            repo = await resolve_repo(session, repo_full_name)
        except RepoNotFoundError:
            logger.warning(
                "test_gen_no_index repo=%s — skipping", repo_full_name
            )
            return {
                "status": "error",
                "reason": "repo_not_indexed",
                "repo": repo_full_name,
                "target_file": target_file,
            }
        repo_name = repo.name
        repo_root = Path(repo.root_path)
        repo_default_branch = repo.default_branch or "main"
        if not repo_root.is_dir():
            return {
                "status": "error",
                "reason": "root_path_missing",
                "repo": repo_full_name,
                "target_file": target_file,
            }

    # If the caller didn't specify a base_branch, use the indexed repo's
    # stored default_branch (Week 10). This is what makes the auto-trigger
    # behave correctly on repos that don't use "main".
    resolved_base_branch = base_branch or repo_default_branch

    # --- Recipe (LLM + 3-gate verify) ---
    try:
        async with OpenRouterClient(
            api_key=settings.openrouter_api_key,
            default_model=resolved_model,
        ) as llm:
            async with SessionLocal() as session:
                try:
                    recipe_result = await run_test_generation(
                        session,
                        repo_name,
                        target_file,
                        llm=llm,
                        repo_root=repo_root,
                        model=resolved_model,
                        test_file_path=test_file_path,
                    )
                except FileNotFoundError as exc:
                    return {
                        "status": "error",
                        "reason": str(exc),
                        "repo": repo_full_name,
                        "target_file": target_file,
                    }
    except Exception as exc:  # noqa: BLE001 — surface LLM/network errors cleanly
        logger.error(
            "test_gen_recipe_failed repo=%s file=%s error=%s",
            repo_full_name,
            target_file,
            exc,
        )
        return {
            "status": "error",
            "reason": f"recipe_failed: {exc}",
            "repo": repo_full_name,
            "target_file": target_file,
        }

    base_summary: dict[str, Any] = {
        "status": "completed",
        "repo": repo_full_name,
        "target_file": target_file,
        "test_file_path": recipe_result.test_file_path,
        "test_content": recipe_result.test_content,
        "verified": recipe_result.verified,
        "verification_errors": list(recipe_result.verification_errors),
        "llm_model": recipe_result.llm_model,
        "covered_symbols": list(recipe_result.covered_symbols),
        "notes": recipe_result.notes,
        "llm_confidence": recipe_result.llm_confidence,
        "confidence": recipe_result.confidence,
        "target_repo": target_repo,
        "base_branch": None,
        "base_sha": None,
        "decisions": [],
    }

    if target_repo is None:
        # Local-only run.
        logger.info(
            "test_gen_local_done repo=%s file=%s verified=%s "
            "blended_conf=%.2f",
            repo_full_name,
            target_file,
            recipe_result.verified,
            recipe_result.confidence,
        )
        return base_summary

    # --- Push flow ---
    owner, repo_short = target_repo.split("/", 1)
    auth = GithubAppAuth.from_files(
        app_id=settings.github_app_id,
        private_key_path=settings.github_app_private_key_path,
    )

    resolved_base_sha = base_sha
    existing_test_sha: str | None = None
    async with GithubClient(auth=auth) as gh:
        if resolved_base_sha is None:
            try:
                ref_info = await gh.get_ref(
                    owner, repo_short, f"heads/{resolved_base_branch}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "test_gen_base_sha_lookup_failed repo=%s "
                    "base_branch=%s error=%s",
                    target_repo,
                    resolved_base_branch,
                    exc,
                )
                base_summary.update(
                    status="error",
                    reason=(
                        f"base_sha_lookup_failed: {exc}"
                    ),
                    base_branch=resolved_base_branch,
                )
                return base_summary
            resolved_base_sha = ref_info.sha

        try:
            existing = await gh.get_contents(
                owner,
                repo_short,
                recipe_result.test_file_path,
                ref=resolved_base_branch,
            )
            existing_test_sha = existing.sha
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                logger.error(
                    "test_gen_get_contents_failed repo=%s path=%s "
                    "status=%s",
                    target_repo,
                    recipe_result.test_file_path,
                    exc.response.status_code,
                )
                base_summary.update(
                    status="error",
                    reason=(
                        f"get_contents_failed: HTTP "
                        f"{exc.response.status_code}"
                    ),
                    base_branch=resolved_base_branch,
                    base_sha=resolved_base_sha,
                )
                return base_summary
            existing_test_sha = None

    artifact = TestGenerationArtifact(
        repo=target_repo,
        base_branch=resolved_base_branch,
        base_sha=resolved_base_sha,
        target_file=target_file,
        test_file_path=recipe_result.test_file_path,
        test_content=recipe_result.test_content,
        existing_test_sha=existing_test_sha,
        fallback_issue=fallback_issue,
        confidence=recipe_result.confidence,
    )
    decisions = build_test_generation_decisions(artifact)

    decision_summaries = await _execute_decision_chain(
        decisions, mode=mode, auth=auth
    )

    base_summary.update(
        base_branch=resolved_base_branch,
        base_sha=resolved_base_sha,
        decisions=decision_summaries,
    )

    final_outcomes = [d["outcome"] for d in decision_summaries]
    logger.info(
        "test_gen_push_done repo=%s file=%s verified=%s "
        "blended_conf=%.2f outcomes=%s",
        target_repo,
        target_file,
        recipe_result.verified,
        recipe_result.confidence,
        ",".join(final_outcomes),
    )
    return base_summary


async def _execute_decision_chain(
    decisions: list[Decision],
    *,
    mode: WriteMode,
    auth: GithubAppAuth,
) -> list[dict[str, Any]]:
    """Execute the test-gen Decisions in order, stopping on non-progress.

    "Non-progress" = anything outside ``EXECUTED`` / ``SHADOW_LOGGED`` /
    ``DEDUPED``. A downgrade or rejection earlier in the chain
    invalidates everything downstream (no point trying to write a file
    onto a branch that wasn't created), so we stop and return what
    happened so far.
    """
    summaries: list[dict[str, Any]] = []
    gh_client: GithubClient | None = None
    if mode != WriteMode.SHADOW:
        gh_client = GithubClient(auth=auth)

    try:
        async with SessionLocal() as session:
            for decision in decisions:
                decision_result = await execute_decision(
                    decision,
                    mode=mode,
                    client=gh_client,
                    session=session,
                    agent="test_generator",
                )
                summaries.append(_decision_summary(decision, decision_result))
                if decision_result.outcome not in _PROGRESS_OUTCOMES:
                    break
            await session.commit()
    finally:
        if gh_client is not None:
            await gh_client.aclose()
    return summaries


def _decision_summary(decision: Decision, result: Any) -> dict[str, Any]:
    """Pull just the fields the CLI/webhook care about out of a result."""
    summary: dict[str, Any] = {
        "action": decision.action,
        "outcome": result.outcome.value,
        "downgrade_reason": result.downgrade_reason,
        "error": result.error,
        "side_effect": dict(result.side_effect or {}),
    }
    if decision.action == "create_branch":
        summary["ref"] = decision.payload.get("ref")
    elif decision.action == "update_file":
        summary["path"] = decision.payload.get("path")
    elif decision.action == "open_pr":
        summary["head"] = decision.payload.get("head")
        summary["base"] = decision.payload.get("base")
    return summary
