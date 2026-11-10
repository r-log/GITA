"""Tests for the "already reviewed this commit?" check in the PR runner.

The check runs before any LLM call and uses the same identity the review
Decision carries (``pr-review:{pr}:{head_sha}``), in the partition of the
current write mode.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.dedupe import record_action
from gita.agents.pr_reviewer.bridge import build_pr_review_decision
from gita.agents.types import PRReviewResult
from gita.jobs.runners import _already_reviewed

SHA_A, SHA_B = "a" * 40, "b" * 40


def _review(verdict: str = "approve", summary: str = "No issues found.") -> PRReviewResult:
    return PRReviewResult(
        repo_name="amass",
        pr_number=42,
        pr_title="t",
        summary=summary,
        verdict=verdict,
        findings=[],
        confidence=0.9,
    )


async def _record(session: AsyncSession, *, head_sha: str, live: bool, agent: str = "pr_reviewer"):
    decision = build_pr_review_decision(
        _review(), repo_full_name="r-log/AMASS", pr_number=42, head_sha=head_sha
    )
    await record_action(
        session,
        decision,
        agent=agent,
        outcome="executed" if live else "shadow_logged",
        live=live,
    )


class TestAlreadyReviewed:
    async def test_no_prior_review(self, db_session: AsyncSession):
        assert not await _already_reviewed(db_session, "r-log/AMASS", 42, SHA_A, live=True)

    async def test_same_commit_already_reviewed(self, db_session: AsyncSession):
        await _record(db_session, head_sha=SHA_A, live=True)
        assert await _already_reviewed(db_session, "r-log/AMASS", 42, SHA_A, live=True)

    async def test_new_commit_is_not_reviewed_yet(self, db_session: AsyncSession):
        await _record(db_session, head_sha=SHA_A, live=True)
        assert not await _already_reviewed(db_session, "r-log/AMASS", 42, SHA_B, live=True)

    async def test_repo_name_is_case_insensitive(self, db_session: AsyncSession):
        await _record(db_session, head_sha=SHA_A, live=True)
        assert await _already_reviewed(db_session, "R-LOG/amass", 42, SHA_A, live=True)

    async def test_other_pr_not_matched(self, db_session: AsyncSession):
        await _record(db_session, head_sha=SHA_A, live=True)
        assert not await _already_reviewed(db_session, "r-log/AMASS", 43, SHA_A, live=True)

    async def test_other_agent_not_matched(self, db_session: AsyncSession):
        await _record(db_session, head_sha=SHA_A, live=True, agent="onboarding")
        assert not await _already_reviewed(db_session, "r-log/AMASS", 42, SHA_A, live=True)

    async def test_shadow_review_does_not_block_live_review(self, db_session: AsyncSession):
        """Observing in shadow, then switching writes on, must still post."""
        await _record(db_session, head_sha=SHA_A, live=False)
        assert not await _already_reviewed(db_session, "r-log/AMASS", 42, SHA_A, live=True)
        assert await _already_reviewed(db_session, "r-log/AMASS", 42, SHA_A, live=False)


class TestReviewIdentity:
    def test_identical_bodies_on_different_commits_are_different_reviews(self):
        """The bug: identity was the first 200 chars of the body, so a clean
        review of commit C deduplicated against the clean review of commit A."""
        from gita.agents.dedupe import compute_signature

        a = build_pr_review_decision(_review(), "r-log/AMASS", 42, head_sha=SHA_A)
        c = build_pr_review_decision(_review(), "r-log/AMASS", 42, head_sha=SHA_B)
        assert a.payload["body"] == c.payload["body"]
        assert compute_signature(a) != compute_signature(c)

    def test_same_commit_different_wording_is_the_same_review(self):
        from gita.agents.dedupe import compute_signature

        a = build_pr_review_decision(_review(summary="Looks fine."), "r-log/AMASS", 42, head_sha=SHA_A)
        b = build_pr_review_decision(_review(summary="All good."), "r-log/AMASS", 42, head_sha=SHA_A)
        assert compute_signature(a) == compute_signature(b)
