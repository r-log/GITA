"""``gita.jobs`` — ARQ job definitions for the webhook worker.

Each public function here is registered with the ARQ worker.
The webhook dispatch layer (``gita.web.dispatch``) builds ``JobRequest``
objects whose ``function_name`` matches the function names exported here.

**Job ID convention (Wall 3):**

Every job gets a deterministic ID so ARQ rejects duplicate enqueues.
The pattern is ``{function}:{repo}:{discriminator}``:

- ``review-pr:r-log/amass:42:<head sha>`` — one review per PR commit
- ``onboard:r-log/amass:7``                — one onboard per issue
- ``reindex:r-log/amass:<sha>``            — one re-index per default-branch push

See ``gita.web.dispatch`` for why each ID has the shape it has.

The actual agent logic lives in ``gita.jobs.runners``. These functions
are thin ARQ wrappers that delegate to the runners.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from arq import Retry

from gita.llm.client import LLMTransientError

logger = logging.getLogger(__name__)

# Backoff between job attempts for transient failures: 30 s, then 60 s.
_RETRY_BASE_SECONDS = 30
_MAX_TRIES = 3  # keep in sync with WorkerSettings.max_tries


def is_transient(exc: BaseException) -> bool:
    """Failures worth retrying the whole job for: provider/GitHub outages."""
    if isinstance(exc, LLMTransientError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError)


async def _with_retry(
    ctx: dict[str, Any], run: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    """Run a job body; turn transient failures into ``arq.Retry``.

    ARQ only retries a job that raises ``Retry`` (``max_tries`` doesn't
    apply to ordinary exceptions), so without this a single 502 from the
    LLM provider or GitHub loses the review for good.
    """
    try:
        return await run()
    except Exception as exc:
        job_try = int(ctx.get("job_try", 1))
        if is_transient(exc) and job_try < _MAX_TRIES:
            defer = _RETRY_BASE_SECONDS * 2 ** (job_try - 1)
            logger.warning("job_retry try=%d defer=%ds reason=%r", job_try, defer, exc)
            raise Retry(defer=defer) from exc
        raise


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

    return await _with_retry(
        ctx, lambda: run_pr_review_job(repo_full_name, pr_number, head_sha=head_sha)
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

    return await _with_retry(ctx, lambda: run_onboarding_job(repo_full_name, issue_number))


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

    return await _with_retry(
        ctx,
        lambda: run_reindex_job(repo_full_name, after_sha=after_sha, redis=ctx.get("redis")),
    )


async def generate_tests(
    ctx: dict[str, Any],
    *,
    repo_full_name: str,
    target_file: str,
    target_repo: str | None = None,
    base_branch: str | None = None,
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
        redis=ctx.get("redis"),
        # Webhook-triggered: the code under test is unreviewed.
        require_isolation=True,
    )


# List of all job functions — used by the ARQ worker settings.
ALL_JOBS = [review_pr, onboard_repo, reindex_repo, generate_tests]
