"""Tests for the event dispatch layer.

Exercises ``dispatch_event`` routing, the handlers, and the job IDs they
produce. No DB, no Redis, no HTTP.
"""
from __future__ import annotations

import pytest

from gita.web.dispatch import (
    EVENT_HANDLERS,
    JobRequest,
    dispatch_event,
    handle_onboarding_label,
    handle_pr_review,
    handle_push_reindex,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------
def _pr_payload(
    *,
    action: str = "opened",
    pr_number: int = 42,
    repo: str = "r-log/AMASS",
    head_sha: str = SHA_A,
    draft: bool = False,
) -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "head": {"sha": head_sha},
            "draft": draft,
        },
        "repository": {"full_name": repo},
        "sender": {"login": "octocat", "type": "User"},
    }


def _label_payload(
    *,
    label: str = "gita:onboard",
    issue_number: int = 7,
    repo: str = "r-log/AMASS",
    sender: str = "maintainer",
    author: str = "reporter",
    association: str = "NONE",
) -> dict:
    return {
        "action": "labeled",
        "label": {"name": label},
        "issue": {
            "number": issue_number,
            "user": {"login": author},
            "author_association": association,
        },
        "repository": {"full_name": repo},
        "sender": {"login": sender, "type": "User"},
    }


def _push_payload(
    *,
    repo: str = "r-log/AMASS",
    ref: str = "refs/heads/main",
    default_branch: str = "main",
    after: str = SHA_A,
    deleted: bool = False,
) -> dict:
    return {
        "ref": ref,
        "after": after,
        "deleted": deleted,
        "repository": {"full_name": repo, "default_branch": default_branch},
        "sender": {"login": "octocat", "type": "User"},
    }


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
class TestDispatchRouting:
    @pytest.mark.parametrize(
        "action", ["opened", "synchronize", "reopened", "ready_for_review"]
    )
    async def test_pull_request_actions(self, action):
        job = await dispatch_event("pull_request", action, _pr_payload(action=action))
        assert job is not None and job.function_name == "review_pr"

    async def test_issue_labeled(self):
        job = await dispatch_event("issues", "labeled", _label_payload())
        assert job is not None and job.function_name == "onboard_repo"

    async def test_issue_opened_is_not_a_trigger(self):
        payload = _label_payload()
        payload["action"] = "opened"
        assert await dispatch_event("issues", "opened", payload) is None

    async def test_push(self):
        job = await dispatch_event("push", None, _push_payload())
        assert job is not None and job.function_name == "reindex_repo"

    @pytest.mark.parametrize(
        ("event", "action"),
        [
            ("star", "created"),
            ("pull_request", "closed"),
            ("issue_comment", "created"),  # GITA's own comments
            ("pull_request_review", "submitted"),
        ],
    )
    async def test_unhandled_events(self, event, action):
        assert await dispatch_event(event, action, {}) is None


# ---------------------------------------------------------------------------
# PR review
# ---------------------------------------------------------------------------
class TestHandlePrReview:
    async def test_job_request_shape(self):
        job = await handle_pr_review(_pr_payload())
        assert isinstance(job, JobRequest)
        assert job.kwargs == {
            "repo_full_name": "r-log/AMASS",
            "pr_number": 42,
            "head_sha": SHA_A,
        }
        assert job.repo_full_name == "r-log/amass"

    async def test_job_id_names_the_commit(self):
        a = await handle_pr_review(_pr_payload(head_sha=SHA_A))
        b = await handle_pr_review(_pr_payload(head_sha=SHA_B))
        assert a.job_id == f"review-pr:r-log/amass:42:{SHA_A}"
        # A new commit on the same PR is new work, not a duplicate.
        assert a.job_id != b.job_id

    async def test_redelivery_has_identical_id(self):
        first = await handle_pr_review(_pr_payload())
        again = await handle_pr_review(_pr_payload())
        assert first.job_id == again.job_id

    async def test_review_is_debounced(self, monkeypatch):
        monkeypatch.setattr("gita.web.dispatch.settings.review_debounce_seconds", 20.0)
        job = await handle_pr_review(_pr_payload())
        assert job.defer_seconds == 20.0

    async def test_debounce_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr("gita.web.dispatch.settings.review_debounce_seconds", 0.0)
        job = await handle_pr_review(_pr_payload())
        assert job.defer_seconds is None

    async def test_draft_prs_are_skipped(self):
        assert await handle_pr_review(_pr_payload(draft=True)) is None

    async def test_missing_fields_return_none(self):
        payload = _pr_payload()
        del payload["pull_request"]["number"]
        assert await handle_pr_review(payload) is None
        payload = _pr_payload()
        del payload["repository"]
        assert await handle_pr_review(payload) is None


# ---------------------------------------------------------------------------
# Onboarding label
# ---------------------------------------------------------------------------
class TestHandleOnboardingLabel:
    async def test_maintainer_labels_someone_elses_issue(self):
        job = await handle_onboarding_label(_label_payload())
        assert job.job_id == "onboard:r-log/amass:7"
        assert job.kwargs == {"repo_full_name": "r-log/AMASS", "issue_number": 7}

    async def test_other_labels_ignored(self):
        assert await handle_onboarding_label(_label_payload(label="bug")) is None

    async def test_template_applied_label_by_outsider_ignored(self):
        """Issue templates can apply labels for anyone; that's no signal."""
        payload = _label_payload(sender="drive-by", author="drive-by", association="NONE")
        assert await handle_onboarding_label(payload) is None

    @pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
    async def test_self_applied_by_trusted_author(self, association):
        payload = _label_payload(sender="dev", author="dev", association=association)
        assert await handle_onboarding_label(payload) is not None

    async def test_label_name_is_configurable(self, monkeypatch):
        monkeypatch.setattr("gita.web.dispatch.settings.onboarding_label", "needs-tour")
        assert await handle_onboarding_label(_label_payload()) is None
        assert await handle_onboarding_label(_label_payload(label="needs-tour")) is not None


# ---------------------------------------------------------------------------
# Push → reindex
# ---------------------------------------------------------------------------
class TestHandlePushReindex:
    async def test_default_branch_push(self):
        job = await handle_push_reindex(_push_payload())
        assert job.job_id == f"reindex:r-log/amass:{SHA_A}"
        assert job.kwargs == {"repo_full_name": "r-log/AMASS", "after_sha": SHA_A}

    async def test_respects_non_main_default_branch(self):
        payload = _push_payload(ref="refs/heads/develop", default_branch="develop")
        assert await handle_push_reindex(payload) is not None

    @pytest.mark.parametrize(
        "payload",
        [
            _push_payload(ref="refs/heads/feature-x"),  # feature branch
            _push_payload(ref="refs/tags/v1.0"),  # tag
            _push_payload(deleted=True, after="0" * 40),  # branch deletion
            _push_payload(after="not-a-sha"),
            _push_payload(default_branch=""),  # can't tell what the default is
        ],
    )
    async def test_everything_else_is_ignored(self, payload):
        assert await handle_push_reindex(payload) is None


def test_registry_is_an_explicit_allowlist():
    assert set(EVENT_HANDLERS) == {
        ("pull_request", "opened"),
        ("pull_request", "synchronize"),
        ("pull_request", "reopened"),
        ("pull_request", "ready_for_review"),
        ("issues", "labeled"),
        ("push", None),
    }
