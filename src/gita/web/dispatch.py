"""Event dispatch — routes GitHub webhook events to handler functions.

**Wall 1 of loop prevention.** Only events explicitly listed in
``EVENT_HANDLERS`` trigger any work. Comment-related events
(``issue_comment.*``, ``pull_request_review.*``) are NOT in the dict,
so when GITA posts a comment, the resulting webhook is silently ignored.

Each handler is an async function that takes the full webhook payload
dict and returns a ``JobRequest`` (what to enqueue) or ``None`` (nothing
to do). The webhook endpoint calls ``dispatch_event``, checks cooldown,
and enqueues the returned job.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


@dataclass
class JobRequest:
    """Describes a job to enqueue in ARQ.

    ``job_id`` is deterministic (Wall 3) — ARQ rejects duplicate enqueues
    when a job with the same ID is already queued or running.
    """

    function_name: str       # ARQ function name, e.g. "review_pr"
    job_id: str              # deterministic, e.g. "review-pr:r-log/amass:42"
    repo_full_name: str      # for cooldown tracking
    kwargs: dict[str, Any]   # passed to the ARQ function


# Type alias for handler functions.
HandlerFunc = Callable[[dict[str, Any]], Coroutine[Any, Any, JobRequest | None]]


# ---------------------------------------------------------------------------
# Individual handlers — extract payload fields and build a JobRequest.
# ---------------------------------------------------------------------------
async def handle_pr_review(payload: dict[str, Any]) -> JobRequest | None:
    """Handle ``pull_request.opened`` and ``pull_request.synchronize``."""
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    pr_number = pr.get("number")
    repo_full_name = repo.get("full_name")

    if not pr_number or not repo_full_name:
        logger.warning("handle_pr_review: missing pr_number or repo")
        return None

    repo_lower = repo_full_name.lower()
    return JobRequest(
        function_name="review_pr",
        job_id=f"review-pr:{repo_lower}:{pr_number}",
        repo_full_name=repo_lower,
        kwargs={
            "repo_full_name": repo_full_name,
            "pr_number": pr_number,
            "head_sha": pr.get("head", {}).get("sha"),
        },
    )


async def handle_onboarding(payload: dict[str, Any]) -> JobRequest | None:
    """Handle ``issues.opened`` — trigger onboarding analysis."""
    issue = payload.get("issue") or {}
    repo = payload.get("repository") or {}
    issue_number = issue.get("number")
    repo_full_name = repo.get("full_name")

    if not issue_number or not repo_full_name:
        logger.warning("handle_onboarding: missing issue_number or repo")
        return None

    repo_lower = repo_full_name.lower()
    return JobRequest(
        function_name="onboard_repo",
        job_id=f"onboard:{repo_lower}:{issue_number}",
        repo_full_name=repo_lower,
        kwargs={
            "repo_full_name": repo_full_name,
            "issue_number": issue_number,
        },
    )


async def handle_push_reindex(payload: dict[str, Any]) -> JobRequest | None:
    """Handle ``push`` — trigger incremental re-index."""
    repo = payload.get("repository") or {}
    repo_full_name = repo.get("full_name")
    after_sha = payload.get("after")

    if not repo_full_name:
        logger.warning("handle_push_reindex: missing repo")
        return None

    repo_lower = repo_full_name.lower()
    return JobRequest(
        function_name="reindex_repo",
        job_id=f"reindex:{repo_lower}:{after_sha or 'unknown'}",
        repo_full_name=repo_lower,
        kwargs={
            "repo_full_name": repo_full_name,
            "after_sha": after_sha,
        },
    )


# ---------------------------------------------------------------------------
# Handler registry (Wall 1 — event type allowlist)
# ---------------------------------------------------------------------------
EVENT_HANDLERS: dict[tuple[str, str | None], HandlerFunc] = {
    ("pull_request", "opened"): handle_pr_review,
    ("pull_request", "synchronize"): handle_pr_review,
    ("issues", "opened"): handle_onboarding,
    ("push", None): handle_push_reindex,
}


async def dispatch_event(
    event_type: str,
    action: str | None,
    payload: dict[str, Any],
) -> JobRequest | None:
    """Route an event to its handler and return a ``JobRequest`` or None.

    Lookup order:
    1. ``(event_type, action)`` — exact match (e.g. ``("pull_request", "opened")``)
    2. ``(event_type, None)`` — wildcard match (e.g. ``("push", None)``)

    If neither matches, returns ``None`` — the event is silently ignored.
    """
    handler = EVENT_HANDLERS.get((event_type, action))
    if handler is None:
        # Try wildcard (event_type, None) — used by push events which
        # have no action field.
        handler = EVENT_HANDLERS.get((event_type, None))

    if handler is None:
        logger.debug(
            "dispatch_no_handler event=%s action=%s",
            event_type,
            action,
        )
        return None

    return await handler(payload)
