"""Tests for the agent runner functions.

Focuses on:
1. **SHA-based skip** — DB-integrated tests against ``agent_actions``.
2. **Runner error paths** — missing credentials, missing index.

The full LLM pipeline is tested by golden tests. These tests use the
test DB but mock external calls (LLM, GitHub API).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import AgentAction
from gita.jobs.runners import (
    _SHA_EVIDENCE_PREFIX,
    _check_sha_already_reviewed,
)


# ---------------------------------------------------------------------------
# SHA-based skip: _check_sha_already_reviewed
# ---------------------------------------------------------------------------
class TestCheckShaAlreadyReviewed:
    async def test_no_prior_review(self, db_session: AsyncSession):
        """First review of a PR — no skip."""
        result = await _check_sha_already_reviewed(
            db_session, "r-log/AMASS", "abc123"
        )
        assert result is False

    async def test_prior_review_with_matching_sha(self, db_session: AsyncSession):
        """Prior review exists with same SHA — should skip."""
        row = AgentAction(
            repo_name="r-log/amass",
            agent="pr_reviewer",
            action="comment",
            signature="fakesig123",
            outcome="executed",
            confidence=0.8,
            evidence=[
                "pr_reviewer verdict: approve",
                f"{_SHA_EVIDENCE_PREFIX}abc123",
            ],
        )
        db_session.add(row)
        await db_session.flush()

        result = await _check_sha_already_reviewed(
            db_session, "r-log/AMASS", "abc123"
        )
        assert result is True

    async def test_prior_review_with_different_sha(self, db_session: AsyncSession):
        """Prior review exists but with a different SHA — no skip."""
        row = AgentAction(
            repo_name="r-log/amass",
            agent="pr_reviewer",
            action="comment",
            signature="fakesig456",
            outcome="executed",
            confidence=0.8,
            evidence=[
                "pr_reviewer verdict: approve",
                f"{_SHA_EVIDENCE_PREFIX}different_sha",
            ],
        )
        db_session.add(row)
        await db_session.flush()

        result = await _check_sha_already_reviewed(
            db_session, "r-log/AMASS", "abc123"
        )
        assert result is False

    async def test_case_insensitive_repo(self, db_session: AsyncSession):
        """Repo name matching is case-insensitive."""
        row = AgentAction(
            repo_name="r-log/amass",
            agent="pr_reviewer",
            action="comment",
            signature="fakesig789",
            outcome="executed",
            confidence=0.8,
            evidence=[f"{_SHA_EVIDENCE_PREFIX}sha999"],
        )
        db_session.add(row)
        await db_session.flush()

        result = await _check_sha_already_reviewed(
            db_session, "R-Log/AMASS", "sha999"
        )
        assert result is True

    async def test_different_agent_not_matched(self, db_session: AsyncSession):
        """Only pr_reviewer rows count — onboarding rows are ignored."""
        row = AgentAction(
            repo_name="r-log/amass",
            agent="onboarding",
            action="comment",
            signature="fakesig_onboard",
            outcome="executed",
            confidence=0.8,
            evidence=[f"{_SHA_EVIDENCE_PREFIX}abc123"],
        )
        db_session.add(row)
        await db_session.flush()

        result = await _check_sha_already_reviewed(
            db_session, "r-log/AMASS", "abc123"
        )
        assert result is False

    async def test_shadow_logged_still_counts(self, db_session: AsyncSession):
        """Shadow-mode reviews should still prevent re-reviews."""
        row = AgentAction(
            repo_name="r-log/amass",
            agent="pr_reviewer",
            action="comment",
            signature="fakesig_shadow",
            outcome="shadow_logged",
            confidence=0.7,
            evidence=[f"{_SHA_EVIDENCE_PREFIX}sha_shadow"],
        )
        db_session.add(row)
        await db_session.flush()

        result = await _check_sha_already_reviewed(
            db_session, "r-log/AMASS", "sha_shadow"
        )
        assert result is True

    async def test_evidence_without_sha_not_matched(self, db_session: AsyncSession):
        """Rows without the SHA evidence tag don't match."""
        row = AgentAction(
            repo_name="r-log/amass",
            agent="pr_reviewer",
            action="comment",
            signature="fakesig_nosha",
            outcome="executed",
            confidence=0.8,
            evidence=["pr_reviewer verdict: approve", "3 findings"],
        )
        db_session.add(row)
        await db_session.flush()

        result = await _check_sha_already_reviewed(
            db_session, "r-log/AMASS", "abc123"
        )
        assert result is False


# ---------------------------------------------------------------------------
# SHA evidence tag format
# ---------------------------------------------------------------------------
class TestShaEvidenceTag:
    def test_prefix_format(self):
        """The prefix is stable — changing it would break existing lookups."""
        assert _SHA_EVIDENCE_PREFIX == "head_sha:"

    def test_tag_construction(self):
        sha = "deadbeef1234"
        tag = f"{_SHA_EVIDENCE_PREFIX}{sha}"
        assert tag == "head_sha:deadbeef1234"
        assert tag.startswith(_SHA_EVIDENCE_PREFIX)
        assert tag[len(_SHA_EVIDENCE_PREFIX):] == sha
