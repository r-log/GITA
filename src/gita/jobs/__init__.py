"""``gita.jobs`` — ARQ job definitions for the webhook worker.

Each public function here is registered with the ARQ worker.
The webhook dispatch layer (``gita.web.dispatch``) builds ``JobRequest``
objects whose ``function_name`` matches the function names exported here.

**Job ID convention (Wall 3):**

Every job gets a deterministic ID so ARQ rejects duplicate enqueues.
The pattern is ``{function}:{repo}:{discriminator}``:

- ``review-pr:r-log/amass:42``  — one review per PR number
- ``onboard:r-log/amass:7``     — one onboard per issue number
- ``reindex:r-log/amass:abc123`` — one re-index per push SHA

The actual agent logic lives in ``gita.jobs.runners``. These functions
are thin ARQ wrappers that delegate to the runners.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def review_pr(
    ctx: dict[str, Any],
    *,
    repo_full_name: str,
    pr_number: int,
    head_sha: str | None = None,
) -> dict[str, Any]:
    """Review a pull request. Enqueued by ``handle_pr_review``."""
    logger.info(
        "job_start function=review_pr repo=%s pr=%d sha=%s",
        repo_full_name,
        pr_number,
        head_sha,
    )
    from gita.jobs.runners import run_pr_review_job

    return await run_pr_review_job(
        repo_full_name, pr_number, head_sha=head_sha
    )


async def onboard_repo(
    ctx: dict[str, Any],
    *,
    repo_full_name: str,
    issue_number: int,
) -> dict[str, Any]:
    """Onboard a repository. Enqueued by ``handle_onboarding``."""
    logger.info(
        "job_start function=onboard_repo repo=%s issue=%d",
        repo_full_name,
        issue_number,
    )
    from gita.jobs.runners import run_onboarding_job

    return await run_onboarding_job(repo_full_name, issue_number)


async def reindex_repo(
    ctx: dict[str, Any],
    *,
    repo_full_name: str,
    after_sha: str | None = None,
) -> dict[str, Any]:
    """Incrementally re-index a repository. Enqueued by ``handle_push_reindex``.

    Passes ``ctx['redis']`` (the ARQ pool ARQ injects into every job's
    context) into the runner so the post-reindex auto-test-generation
    trigger can enqueue follow-up ``generate_tests`` jobs without
    opening its own Redis connection.
    """
    logger.info(
        "job_start function=reindex_repo repo=%s sha=%s",
        repo_full_name,
        after_sha,
    )
    from gita.jobs.runners import run_reindex_job

    return await run_reindex_job(
        repo_full_name, after_sha=after_sha, redis=ctx.get("redis")
    )


async def generate_tests(
    ctx: dict[str, Any],
    *,
    repo_full_name: str,
    target_file: str,
    target_repo: str | None = None,
    base_branch: str = "main",
    base_sha: str | None = None,
    fallback_issue: int | None = None,
    test_file_path: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Generate + push tests for one target file. Enqueued by ``run_reindex_job``.

    Job ID convention: ``generate-tests:{repo_lower}:{target_file}:{sha7}``
    so retrying the same target on the same SHA hits ARQ-level dedupe.
    """
    logger.info(
        "job_start function=generate_tests repo=%s file=%s sha=%s",
        repo_full_name,
        target_file,
        (base_sha or "?")[:7],
    )
    from gita.jobs.runners import run_test_generation_job

    return await run_test_generation_job(
        repo_full_name,
        target_file,
        target_repo=target_repo,
        base_branch=base_branch,
        base_sha=base_sha,
        fallback_issue=fallback_issue,
        test_file_path=test_file_path,
        model=model,
    )


# List of all job functions — used by the ARQ worker settings.
ALL_JOBS = [review_pr, onboard_repo, reindex_repo, generate_tests]
