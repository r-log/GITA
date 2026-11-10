"""Event dispatch — routes GitHub webhook events to job requests.

Only events listed in ``EVENT_HANDLERS`` trigger work; everything else
(including the ``issue_comment`` events GITA's own comments produce) is
ignored. Each handler turns a payload into a ``JobRequest`` or ``None``.

Job identity is chosen so that ARQ's job-ID dedupe drops exactly the
duplicates and nothing else. ARQ refuses an enqueue while a job with the
same ID is queued or running *or its result is still stored*
(``keep_result``), so an ID must name the unit of work precisely:

* review — ``review-pr:{repo}:{pr}:{head_sha}``. A redelivered event for
  the same commit is dropped; a new commit is a new job. Reviews are
  deferred by ``REVIEW_DEBOUNCE_SECONDS`` so a burst of pushes collapses:
  each job re-reads the PR's head when it runs and skips itself if a
  newer commit has superseded it.
* reindex — ``reindex:{repo}:{after_sha}``, only for pushes to the
  repository's default branch. The index is the ground truth for
  reviews; it must not follow feature branches or tags.
* onboarding — ``onboard:{repo}:{issue}``, only when the configured label
  is applied by someone trusted (see ``handle_onboarding_label``).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from gita.config import settings
from gita.jobs.ids import onboarding_job_id, reindex_job_id, review_job_id

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# ``author_association`` values that mean "has a relationship with the
# repo", as opposed to CONTRIBUTOR / FIRST_TIMER / NONE.
TRUSTED_AUTHOR_ASSOCIATIONS: frozenset[str] = frozenset(
    {"OWNER", "MEMBER", "COLLABORATOR"}
)


@dataclass
class JobRequest:
    """Describes a job to enqueue in ARQ."""

    function_name: str       # ARQ function name, e.g. "review_pr"
    job_id: str              # deterministic; see module docstring
    repo_full_name: str
    kwargs: dict[str, Any]   # passed to the ARQ function
    defer_seconds: float | None = None


HandlerFunc = Callable[[dict[str, Any]], Coroutine[Any, Any, JobRequest | None]]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def handle_pr_review(payload: dict[str, Any]) -> JobRequest | None:
    """``pull_request`` opened / synchronize / reopened / ready_for_review."""
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    pr_number = pr.get("number")
    repo_full_name = repo.get("full_name")

    if not pr_number or not repo_full_name:
        logger.warning("handle_pr_review: missing pr_number or repo")
        return None
    if pr.get("draft"):
        logger.info("pr_review_skipped_draft repo=%s pr=%s", repo_full_name, pr_number)
        return None

    head_sha = (pr.get("head") or {}).get("sha")
    return JobRequest(
        function_name="review_pr",
        job_id=review_job_id(repo_full_name, pr_number, head_sha),
        repo_full_name=repo_full_name.lower(),
        kwargs={
            "repo_full_name": repo_full_name,
            "pr_number": pr_number,
            "head_sha": head_sha,
        },
        defer_seconds=settings.review_debounce_seconds or None,
    )


async def handle_onboarding_label(payload: dict[str, Any]) -> JobRequest | None:
    """``issues.labeled`` with the onboarding label, applied by someone trusted.

    Applying a label normally needs triage permission, which is what
    makes a label a reasonable trigger for a job that spends LLM tokens
    and posts a long comment. The exception is an issue template that
    applies the label automatically: then the *author* is the sender, and
    anyone can open an issue. So a self-applied label only counts when
    the author is an owner, member or collaborator.
    """
    label = (payload.get("label") or {}).get("name")
    if not label or label != settings.onboarding_label:
        return None

    issue = payload.get("issue") or {}
    repo = payload.get("repository") or {}
    issue_number = issue.get("number")
    repo_full_name = repo.get("full_name")
    if not issue_number or not repo_full_name:
        logger.warning("handle_onboarding_label: missing issue_number or repo")
        return None

    sender_login = (payload.get("sender") or {}).get("login")
    author_login = (issue.get("user") or {}).get("login")
    association = issue.get("author_association", "NONE")
    if sender_login == author_login and association not in TRUSTED_AUTHOR_ASSOCIATIONS:
        logger.info(
            "onboarding_ignored_untrusted_self_label repo=%s issue=%s "
            "author=%s association=%s",
            repo_full_name,
            issue_number,
            author_login,
            association,
        )
        return None

    return JobRequest(
        function_name="onboard_repo",
        job_id=onboarding_job_id(repo_full_name, issue_number),
        repo_full_name=repo_full_name.lower(),
        kwargs={
            "repo_full_name": repo_full_name,
            "issue_number": issue_number,
        },
    )


async def handle_push_reindex(payload: dict[str, Any]) -> JobRequest | None:
    """``push`` to the default branch — incremental re-index."""
    repo = payload.get("repository") or {}
    repo_full_name = repo.get("full_name")
    if not repo_full_name:
        logger.warning("handle_push_reindex: missing repo")
        return None

    ref = payload.get("ref")
    default_branch = repo.get("default_branch")
    after_sha = payload.get("after") or ""

    if payload.get("deleted"):
        return None  # branch or tag deletion: nothing to index
    if not default_branch or ref != f"refs/heads/{default_branch}":
        logger.debug(
            "push_ignored_non_default_ref repo=%s ref=%s default=%s",
            repo_full_name,
            ref,
            default_branch,
        )
        return None
    if not _SHA_RE.match(after_sha):
        logger.warning(
            "push_ignored_bad_sha repo=%s after=%r", repo_full_name, after_sha
        )
        return None

    return JobRequest(
        function_name="reindex_repo",
        job_id=reindex_job_id(repo_full_name, after_sha),
        repo_full_name=repo_full_name.lower(),
        kwargs={
            "repo_full_name": repo_full_name,
            "after_sha": after_sha,
        },
    )


# ---------------------------------------------------------------------------
# Registry (event-type allowlist)
# ---------------------------------------------------------------------------
EVENT_HANDLERS: dict[tuple[str, str | None], HandlerFunc] = {
    ("pull_request", "opened"): handle_pr_review,
    ("pull_request", "synchronize"): handle_pr_review,
    ("pull_request", "reopened"): handle_pr_review,
    ("pull_request", "ready_for_review"): handle_pr_review,
    ("issues", "labeled"): handle_onboarding_label,
    ("push", None): handle_push_reindex,
}


async def dispatch_event(
    event_type: str,
    action: str | None,
    payload: dict[str, Any],
) -> JobRequest | None:
    """Route an event to its handler and return a ``JobRequest`` or None.

    Lookup is ``(event_type, action)`` first, then ``(event_type, None)``
    for events without an action (``push``).
    """
    handler = EVENT_HANDLERS.get((event_type, action))
    if handler is None:
        handler = EVENT_HANDLERS.get((event_type, None))
    if handler is None:
        logger.debug("dispatch_no_handler event=%s action=%s", event_type, action)
        return None
    return await handler(payload)
