"""Tests for the event dispatch layer.

Exercises ``dispatch_event`` routing, individual handler functions,
and the ``JobRequest`` shapes they produce. No DB, no Redis, no HTTP.
"""
from __future__ import annotations

import pytest

from gita.web.dispatch import (
    EVENT_HANDLERS,
    JobRequest,
    dispatch_event,
    handle_onboarding,
    handle_pr_review,
    handle_push_reindex,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pr_payload(
    *,
    action: str = "opened",
    pr_number: int = 42,
    repo: str = "r-log/AMASS",
    head_sha: str = "abc123",
) -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "head": {"sha": head_sha},
        },
        "repository": {"full_name": repo},
        "sender": {"login": "octocat", "type": "User"},
    }


def _issue_payload(
    *,
    action: str = "opened",
    issue_number: int = 7,
    repo: str = "r-log/AMASS",
) -> dict:
    return {
        "action": action,
        "issue": {"number": issue_number},
        "repository": {"full_name": repo},
        "sender": {"login": "octocat", "type": "User"},
    }


def _push_payload(
    *, repo: str = "r-log/AMASS", after_sha: str = "def456"
) -> dict:
    return {
        "after": after_sha,
        "repository": {"full_name": repo},
        "sender": {"login": "octocat", "type": "User"},
    }


# ---------------------------------------------------------------------------
# dispatch_event routing
# ---------------------------------------------------------------------------
class TestDispatchRouting:
    async def test_pull_request_opened(self):
        job = await dispatch_event("pull_request", "opened", _pr_payload())
        assert job is not None
        assert job.function_name == "review_pr"

    async def test_pull_request_synchronize(self):
        job = await dispatch_event(
            "pull_request", "synchronize", _pr_payload(action="synchronize")
        )
        assert job is not None
        assert job.function_name == "review_pr"

    async def test_issues_opened(self):
        job = await dispatch_event("issues", "opened", _issue_payload())
        assert job is not None
        assert job.function_name == "onboard_repo"

    async def test_push_event(self):
        job = await dispatch_event("push", None, _push_payload())
        assert job is not None
        assert job.function_name == "reindex_repo"

    async def test_unknown_event_returns_none(self):
        job = await dispatch_event("star", "created", {})
        assert job is None

    async def test_unhandled_action_returns_none(self):
        """pull_request.closed is not in EVENT_HANDLERS."""
        job = await dispatch_event("pull_request", "closed", _pr_payload())
        assert job is None

    async def test_issue_comment_not_handled(self):
        """Wall 1: comment events must NOT be handled (loop prevention)."""
        job = await dispatch_event("issue_comment", "created", {})
        assert job is None

    async def test_pr_review_event_not_handled(self):
        """Wall 1: PR review events must NOT be handled."""
        job = await dispatch_event("pull_request_review", "submitted", {})
        assert job is None


# ---------------------------------------------------------------------------
# handle_pr_review
# ---------------------------------------------------------------------------
class TestHandlePrReview:
    async def test_returns_job_request(self):
        job = await handle_pr_review(_pr_payload(pr_number=99, repo="r-log/AMASS"))
        assert isinstance(job, JobRequest)
        assert job.function_name == "review_pr"
        assert job.kwargs["pr_number"] == 99
        assert job.kwargs["repo_full_name"] == "r-log/AMASS"

    async def test_job_id_is_deterministic(self):
        job = await handle_pr_review(_pr_payload(pr_number=42, repo="r-log/AMASS"))
        assert job is not None
        assert job.job_id == "review-pr:r-log/amass:42"

    async def test_job_id_lowercased(self):
        job = await handle_pr_review(_pr_payload(repo="R-Log/AMASS"))
        assert job is not None
        assert "r-log/amass" in job.job_id

    async def test_head_sha_forwarded(self):
        job = await handle_pr_review(_pr_payload(head_sha="deadbeef"))
        assert job is not None
        assert job.kwargs["head_sha"] == "deadbeef"

    async def test_missing_pr_number_returns_none(self):
        payload = _pr_payload()
        payload["pull_request"] = {}
        job = await handle_pr_review(payload)
        assert job is None

    async def test_missing_repo_returns_none(self):
        payload = _pr_payload()
        payload["repository"] = {}
        job = await handle_pr_review(payload)
        assert job is None


# ---------------------------------------------------------------------------
# handle_onboarding
# ---------------------------------------------------------------------------
class TestHandleOnboarding:
    async def test_returns_job_request(self):
        job = await handle_onboarding(_issue_payload(issue_number=7))
        assert isinstance(job, JobRequest)
        assert job.function_name == "onboard_repo"
        assert job.kwargs["issue_number"] == 7

    async def test_job_id_is_deterministic(self):
        job = await handle_onboarding(_issue_payload(issue_number=7, repo="r-log/AMASS"))
        assert job is not None
        assert job.job_id == "onboard:r-log/amass:7"

    async def test_missing_issue_returns_none(self):
        payload = _issue_payload()
        payload["issue"] = {}
        job = await handle_onboarding(payload)
        assert job is None


# ---------------------------------------------------------------------------
# handle_push_reindex
# ---------------------------------------------------------------------------
class TestHandlePushReindex:
    async def test_returns_job_request(self):
        job = await handle_push_reindex(_push_payload(after_sha="abc123"))
        assert isinstance(job, JobRequest)
        assert job.function_name == "reindex_repo"
        assert job.kwargs["after_sha"] == "abc123"

    async def test_job_id_includes_sha(self):
        job = await handle_push_reindex(_push_payload(after_sha="abc123"))
        assert job is not None
        assert job.job_id == "reindex:r-log/amass:abc123"

    async def test_missing_sha_uses_unknown(self):
        payload = _push_payload()
        payload.pop("after", None)
        job = await handle_push_reindex(payload)
        assert job is not None
        assert "unknown" in job.job_id

    async def test_missing_repo_returns_none(self):
        payload = _push_payload()
        payload["repository"] = {}
        job = await handle_push_reindex(payload)
        assert job is None


# ---------------------------------------------------------------------------
# EVENT_HANDLERS registry
# ---------------------------------------------------------------------------
class TestEventHandlersRegistry:
    def test_only_expected_events_registered(self):
        """Verify the allowlist hasn't grown unexpectedly."""
        expected_keys = {
            ("pull_request", "opened"),
            ("pull_request", "synchronize"),
            ("issues", "opened"),
            ("push", None),
        }
        assert set(EVENT_HANDLERS.keys()) == expected_keys

    def test_no_comment_events_in_registry(self):
        """Explicit Wall 1 guard — these must NEVER appear."""
        dangerous = [
            ("issue_comment", "created"),
            ("issue_comment", "edited"),
            ("pull_request_review", "submitted"),
            ("pull_request_review_comment", "created"),
        ]
        for key in dangerous:
            assert key not in EVENT_HANDLERS, f"{key} must not be in EVENT_HANDLERS"
