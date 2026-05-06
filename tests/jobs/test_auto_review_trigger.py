"""Tests for the post-test-gen auto-review trigger (Week 10).

Covers ``_maybe_enqueue_auto_review`` directly. Each case feeds in a
synthetic ``decision_summaries`` list (matching the shape produced by
``_decision_summary``) plus a fake redis pool, and asserts on the
returned summary + captured enqueue calls.

The helper is pure (no DB, no GitHub) — exercising it as a unit lets
us nail every skip path without spinning up the full test-gen pipeline.
"""
from __future__ import annotations

import pytest

from gita.agents.decisions import Outcome
from gita.jobs.runners import _maybe_enqueue_auto_review


# ---------------------------------------------------------------------------
# Fake redis pool — same shape as the one in test_auto_test_gen_trigger.py
# ---------------------------------------------------------------------------
class FakeRedisPool:
    """Captures enqueue_job calls. Optionally returns None on dedupe."""

    def __init__(self, dedupe_after: int | None = None) -> None:
        self.calls: list[dict] = []
        self.dedupe_after = dedupe_after

    async def enqueue_job(
        self,
        function_name: str,
        *,
        _job_id: str,
        **kwargs,
    ):
        self.calls.append(
            {"function": function_name, "job_id": _job_id, "kwargs": kwargs}
        )
        if (
            self.dedupe_after is not None
            and len(self.calls) > self.dedupe_after
        ):
            return None
        return object()  # truthy marker — emulates an arq Job


class RaisingRedisPool:
    """enqueue_job always raises — emulates a transient Redis failure."""

    async def enqueue_job(self, *args, **kwargs):
        raise RuntimeError("redis is down")


# ---------------------------------------------------------------------------
# Decision-summary builders — match the shape of ``_decision_summary``.
# ---------------------------------------------------------------------------
def _create_branch_summary(*, outcome: Outcome = Outcome.EXECUTED) -> dict:
    return {
        "action": "create_branch",
        "outcome": outcome.value,
        "downgrade_reason": None,
        "error": None,
        "side_effect": {},
        "ref": "gita/test-gen-foo-py",
    }


def _update_file_summary(*, outcome: Outcome = Outcome.EXECUTED) -> dict:
    return {
        "action": "update_file",
        "outcome": outcome.value,
        "downgrade_reason": None,
        "error": None,
        "side_effect": {},
        "path": "tests/test_foo.py",
    }


def _open_pr_summary(
    *,
    outcome: Outcome = Outcome.EXECUTED,
    number: int | None = 42,
) -> dict:
    side_effect: dict = {
        "kind": "open_pr",
        "html_url": "https://github.com/owner/repo/pull/42",
        "head": "gita/test-gen-foo-py",
        "base": "main",
        "state": "open",
        "repo": "owner/repo",
    }
    if number is not None:
        side_effect["number"] = number
    return {
        "action": "open_pr",
        "outcome": outcome.value,
        "downgrade_reason": None,
        "error": None,
        "side_effect": side_effect,
        "head": "gita/test-gen-foo-py",
        "base": "main",
    }


def _full_chain(
    *,
    pr_outcome: Outcome = Outcome.EXECUTED,
    pr_number: int | None = 42,
) -> list[dict]:
    return [
        _create_branch_summary(),
        _update_file_summary(),
        _open_pr_summary(outcome=pr_outcome, number=pr_number),
    ]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
class TestEnqueueHappyPath:
    async def test_executed_open_pr_enqueues_review(self):
        pool = FakeRedisPool()

        summary = await _maybe_enqueue_auto_review(
            decision_summaries=_full_chain(),
            target_repo="Owner/Repo",
            redis=pool,
        )

        assert summary["status"] == "enqueued"
        assert summary["pr_number"] == 42
        assert summary["job_id"] == "review-pr:owner/repo:42"
        assert summary["deduped"] is False

        assert len(pool.calls) == 1
        call = pool.calls[0]
        assert call["function"] == "review_pr"
        assert call["job_id"] == "review-pr:owner/repo:42"
        assert call["kwargs"]["repo_full_name"] == "Owner/Repo"
        assert call["kwargs"]["pr_number"] == 42
        # head_sha is intentionally not threaded — see helper docstring.
        assert "head_sha" not in call["kwargs"]

    async def test_repo_lowercase_normalisation_in_job_id(self):
        """Job ID uses lowercased repo so case-only differences collapse."""
        pool = FakeRedisPool()
        summary = await _maybe_enqueue_auto_review(
            decision_summaries=_full_chain(),
            target_repo="OWNER/REPO",
            redis=pool,
        )
        assert summary["job_id"] == "review-pr:owner/repo:42"


# ---------------------------------------------------------------------------
# Skip paths — every "no-fire" reason should be recorded explicitly so
# the structured-log story stays grep-friendly.
# ---------------------------------------------------------------------------
class TestSkipPaths:
    async def test_no_redis_pool_skips(self):
        summary = await _maybe_enqueue_auto_review(
            decision_summaries=_full_chain(),
            target_repo="owner/repo",
            redis=None,
        )
        assert summary == {"status": "skipped", "reason": "no_redis_pool"}

    async def test_no_target_repo_skips(self):
        pool = FakeRedisPool()
        summary = await _maybe_enqueue_auto_review(
            decision_summaries=_full_chain(),
            target_repo=None,
            redis=pool,
        )
        assert summary["status"] == "skipped"
        assert summary["reason"] == "no_target_repo"
        assert pool.calls == []

    async def test_empty_target_repo_skips(self):
        """Defensive: a blank target_repo is treated like None."""
        pool = FakeRedisPool()
        summary = await _maybe_enqueue_auto_review(
            decision_summaries=_full_chain(),
            target_repo="",
            redis=pool,
        )
        assert summary["status"] == "skipped"
        assert summary["reason"] == "no_target_repo"

    async def test_chain_stopped_before_open_pr_skips(self):
        """create_branch + update_file but no open_pr (e.g. update_file
        rejected upstream) — nothing to review."""
        pool = FakeRedisPool()
        summary = await _maybe_enqueue_auto_review(
            decision_summaries=[
                _create_branch_summary(),
                _update_file_summary(),
            ],
            target_repo="owner/repo",
            redis=pool,
        )
        assert summary["status"] == "skipped"
        assert summary["reason"] == "no_open_pr_decision"
        assert pool.calls == []

    async def test_open_pr_shadow_logged_skips(self):
        """WriteMode.SHADOW means no actual PR exists — don't review."""
        pool = FakeRedisPool()
        summary = await _maybe_enqueue_auto_review(
            decision_summaries=_full_chain(pr_outcome=Outcome.SHADOW_LOGGED),
            target_repo="owner/repo",
            redis=pool,
        )
        assert summary["status"] == "skipped"
        assert summary["reason"] == "outcome=shadow_logged"
        assert pool.calls == []

    async def test_open_pr_deduped_skips(self):
        """A deduped open_pr means we re-ran with no real PR creation."""
        pool = FakeRedisPool()
        summary = await _maybe_enqueue_auto_review(
            decision_summaries=_full_chain(pr_outcome=Outcome.DEDUPED),
            target_repo="owner/repo",
            redis=pool,
        )
        assert summary["status"] == "skipped"
        assert summary["reason"] == "outcome=deduped"
        assert pool.calls == []

    async def test_open_pr_executed_without_number_skips(self):
        """Defensive: an executed open_pr with no PR number (shouldn't
        happen in practice) is treated as a skip rather than enqueuing
        a malformed job."""
        pool = FakeRedisPool()
        summary = await _maybe_enqueue_auto_review(
            decision_summaries=_full_chain(pr_number=None),
            target_repo="owner/repo",
            redis=pool,
        )
        assert summary["status"] == "skipped"
        assert summary["reason"] == "no_pr_number"
        assert pool.calls == []


# ---------------------------------------------------------------------------
# Dedupe + error paths
# ---------------------------------------------------------------------------
class TestDedupeAndErrors:
    async def test_arq_level_dedupe_marked_in_summary(self):
        """A repeat enqueue for the same PR collapses at the broker level
        (arq returns None). The summary still says ``enqueued`` but
        flags ``deduped=True`` so log-readers can tell."""
        pool = FakeRedisPool(dedupe_after=0)  # all calls return None
        summary = await _maybe_enqueue_auto_review(
            decision_summaries=_full_chain(),
            target_repo="owner/repo",
            redis=pool,
        )
        assert summary["status"] == "enqueued"
        assert summary["deduped"] is True
        assert summary["job_id"] == "review-pr:owner/repo:42"

    async def test_enqueue_exception_recorded_not_raised(self):
        """A redis outage shouldn't crash the test-gen runner."""
        summary = await _maybe_enqueue_auto_review(
            decision_summaries=_full_chain(),
            target_repo="owner/repo",
            redis=RaisingRedisPool(),
        )
        assert summary["status"] == "enqueue_error"
        assert "redis is down" in summary["reason"]
        assert summary["job_id"] == "review-pr:owner/repo:42"
        assert summary["pr_number"] == 42


# ---------------------------------------------------------------------------
# Job ID determinism — same PR always produces the same job_id, which
# is what makes ARQ-level dedupe work for retries.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "repo,number,expected",
    [
        ("owner/repo", 1, "review-pr:owner/repo:1"),
        ("owner/repo", 999, "review-pr:owner/repo:999"),
        ("R-Log/Amass", 42, "review-pr:r-log/amass:42"),
        ("  owner/repo  ", 7, "review-pr:owner/repo:7"),
    ],
)
async def test_job_id_shape(repo, number, expected):
    pool = FakeRedisPool()
    summary = await _maybe_enqueue_auto_review(
        decision_summaries=_full_chain(pr_number=number),
        target_repo=repo,
        redis=pool,
    )
    assert summary["job_id"] == expected
    assert pool.calls[0]["job_id"] == expected
