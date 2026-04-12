"""Tests for the dedupe module.

Two layers:

1. **Pure signature computation.** Every action shape has a dedicated test
   asserting (a) it produces a deterministic hex string, (b) it changes when
   the identifying field changes, (c) it does NOT change on whitespace/case
   variation where normalization applies.

2. **DB round-trip.** ``check_signature`` + ``record_action`` against the real
   test database. Covers the empty-DB case, the seen-signature case, the
   unique-constraint enforcement, and case-insensitive repo normalization.

Day 2 of Week 3 will bolt these onto ``execute_decision``; Day 1 just proves
each piece in isolation so the integration doesn't have to debug two bugs at
once.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.decisions import Decision
from gita.agents.dedupe import (
    _repo_for_signature,
    check_signature,
    compute_signature,
    record_action,
)
from gita.db.models import AgentAction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _decision(
    action: str,
    *,
    repo: str = "owner/repo",
    issue: int | None = 42,
    payload: dict | None = None,
    confidence: float = 0.9,
    evidence: list[str] | None = None,
) -> Decision:
    target: dict = {"repo": repo}
    if issue is not None:
        target["issue"] = issue
    return Decision(
        action=action,
        target=target,
        payload=payload or {},
        evidence=evidence or ["evidence one"],
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Pure signature computation — one class per action
# ---------------------------------------------------------------------------
class TestSignatureIsHexString:
    def test_returns_64_char_hex(self):
        sig = compute_signature(
            _decision("comment", payload={"body": "hello"})
        )
        assert len(sig) == 64
        int(sig, 16)  # must parse as hex

    def test_deterministic(self):
        d = _decision("comment", payload={"body": "hello"})
        assert compute_signature(d) == compute_signature(d)


class TestCreateIssueSignature:
    def test_title_identifies(self):
        a = _decision(
            "create_issue",
            issue=None,
            payload={"title": "Fix SQL injection", "body": "long body"},
        )
        b = _decision(
            "create_issue",
            issue=None,
            payload={"title": "Fix SQL injection", "body": "DIFFERENT body"},
        )
        # Same title → same signature even with different bodies.
        assert compute_signature(a) == compute_signature(b)

    def test_different_title_different_signature(self):
        a = _decision(
            "create_issue",
            issue=None,
            payload={"title": "Fix SQL injection", "body": "x"},
        )
        b = _decision(
            "create_issue",
            issue=None,
            payload={"title": "Fix XSS", "body": "x"},
        )
        assert compute_signature(a) != compute_signature(b)

    def test_title_is_case_insensitive(self):
        a = _decision(
            "create_issue",
            issue=None,
            payload={"title": "Fix SQL injection"},
        )
        b = _decision(
            "create_issue",
            issue=None,
            payload={"title": "fix sql INJECTION"},
        )
        assert compute_signature(a) == compute_signature(b)

    def test_title_is_whitespace_stripped(self):
        a = _decision(
            "create_issue", issue=None, payload={"title": "Fix bug"}
        )
        b = _decision(
            "create_issue", issue=None, payload={"title": "  Fix bug  "}
        )
        assert compute_signature(a) == compute_signature(b)


class TestCommentSignature:
    def test_same_body_and_target_same_signature(self):
        a = _decision("comment", payload={"body": "Needs review"})
        b = _decision("comment", payload={"body": "Needs review"})
        assert compute_signature(a) == compute_signature(b)

    def test_different_issue_different_signature(self):
        a = _decision("comment", issue=1, payload={"body": "hi"})
        b = _decision("comment", issue=2, payload={"body": "hi"})
        assert compute_signature(a) != compute_signature(b)

    def test_body_within_cap_differs(self):
        a = _decision("comment", payload={"body": "body one"})
        b = _decision("comment", payload={"body": "body two"})
        assert compute_signature(a) != compute_signature(b)

    def test_body_beyond_cap_collapses(self):
        """Bodies that differ only *after* the 200-char cap dedupe."""
        prefix = "x" * 200
        a = _decision("comment", payload={"body": prefix + "AAAAA"})
        b = _decision("comment", payload={"body": prefix + "BBBBB"})
        assert compute_signature(a) == compute_signature(b)


class TestCloseIssueSignature:
    def test_same_target_same_signature(self):
        a = _decision("close_issue", payload={})
        b = _decision("close_issue", payload={})
        assert compute_signature(a) == compute_signature(b)

    def test_different_issue_different_signature(self):
        a = _decision("close_issue", issue=1, payload={})
        b = _decision("close_issue", issue=2, payload={})
        assert compute_signature(a) != compute_signature(b)

    def test_ignores_payload(self):
        """close_issue has no identifying payload; noise shouldn't matter."""
        a = _decision("close_issue", payload={})
        b = _decision("close_issue", payload={"reason": "stale"})
        assert compute_signature(a) == compute_signature(b)


class TestEditIssueSignature:
    def test_same_body_and_title_same_signature(self):
        a = _decision(
            "edit_issue",
            payload={"title": "New title", "body": "New body"},
        )
        b = _decision(
            "edit_issue",
            payload={"title": "New title", "body": "New body"},
        )
        assert compute_signature(a) == compute_signature(b)

    def test_different_body_different_signature(self):
        a = _decision("edit_issue", payload={"title": "T", "body": "one"})
        b = _decision("edit_issue", payload={"title": "T", "body": "two"})
        assert compute_signature(a) != compute_signature(b)

    def test_different_title_different_signature(self):
        a = _decision("edit_issue", payload={"title": "A", "body": "b"})
        b = _decision("edit_issue", payload={"title": "B", "body": "b"})
        assert compute_signature(a) != compute_signature(b)


class TestAddLabelSignature:
    def test_same_labels_same_signature(self):
        a = _decision("add_label", payload={"labels": ["bug", "critical"]})
        b = _decision("add_label", payload={"labels": ["bug", "critical"]})
        assert compute_signature(a) == compute_signature(b)

    def test_label_order_does_not_matter(self):
        a = _decision("add_label", payload={"labels": ["bug", "critical"]})
        b = _decision("add_label", payload={"labels": ["critical", "bug"]})
        assert compute_signature(a) == compute_signature(b)

    def test_different_label_set_different_signature(self):
        a = _decision("add_label", payload={"labels": ["bug"]})
        b = _decision("add_label", payload={"labels": ["bug", "critical"]})
        assert compute_signature(a) != compute_signature(b)


class TestRemoveLabelSignature:
    def test_same_label_same_signature(self):
        a = _decision("remove_label", payload={"label": "stale"})
        b = _decision("remove_label", payload={"label": "stale"})
        assert compute_signature(a) == compute_signature(b)

    def test_different_label_different_signature(self):
        a = _decision("remove_label", payload={"label": "stale"})
        b = _decision("remove_label", payload={"label": "wontfix"})
        assert compute_signature(a) != compute_signature(b)


class TestSignaturesDifferAcrossActions:
    def test_same_repo_different_action_different_signature(self):
        """A close_issue and a comment on the same issue must not collide."""
        a = _decision("close_issue", issue=42, payload={})
        b = _decision("comment", issue=42, payload={"body": ""})
        assert compute_signature(a) != compute_signature(b)


class TestRepoNormalization:
    def test_case_insensitive_repo(self):
        a = _decision("close_issue", repo="Owner/Repo")
        b = _decision("close_issue", repo="owner/repo")
        assert compute_signature(a) == compute_signature(b)

    def test_whitespace_stripped(self):
        a = _decision("close_issue", repo="owner/repo")
        b = _decision("close_issue", repo="  owner/repo  ")
        assert compute_signature(a) == compute_signature(b)

    def test_missing_repo_raises(self):
        d = Decision(
            action="close_issue",
            target={"issue": 1},  # no repo
            payload={},
            confidence=0.9,
        )
        with pytest.raises(ValueError, match="must include 'repo'"):
            compute_signature(d)

    def test_repo_for_signature_helper(self):
        d = _decision("close_issue", repo="Owner/Repo")
        assert _repo_for_signature(d) == "owner/repo"


class TestUnknownAction:
    def test_unknown_action_raises(self):
        d = Decision(
            action="teleport",
            target={"repo": "a/b"},
            payload={},
            confidence=0.9,
        )
        with pytest.raises(ValueError, match="no signature shape"):
            compute_signature(d)


# ---------------------------------------------------------------------------
# DB round-trip
# ---------------------------------------------------------------------------
class TestCheckSignatureEmpty:
    async def test_returns_none_on_empty_db(self, db_session: AsyncSession):
        d = _decision(
            "create_issue", issue=None, payload={"title": "Fix bug"}
        )
        result = await check_signature(db_session, d, agent="onboarding")
        assert result is None


class TestRecordAndCheck:
    async def test_record_then_check_returns_row(
        self, db_session: AsyncSession
    ):
        d = _decision(
            "create_issue", issue=None, payload={"title": "Fix bug"}
        )
        recorded = await record_action(
            db_session,
            d,
            agent="onboarding",
            outcome="executed",
            external_id="gh_issue_123",
        )
        await db_session.commit()

        assert recorded.id is not None
        assert recorded.repo_name == "owner/repo"
        assert recorded.agent == "onboarding"
        assert recorded.action == "create_issue"
        assert recorded.outcome == "executed"
        assert recorded.external_id == "gh_issue_123"
        assert recorded.confidence == 0.9
        assert recorded.evidence == ["evidence one"]

        found = await check_signature(db_session, d, agent="onboarding")
        assert found is not None
        assert found.id == recorded.id

    async def test_check_is_scoped_by_agent(
        self, db_session: AsyncSession
    ):
        """A second agent running the same signature must not dedupe."""
        d = _decision(
            "create_issue", issue=None, payload={"title": "Fix bug"}
        )
        await record_action(
            db_session, d, agent="onboarding", outcome="executed"
        )
        await db_session.commit()

        found = await check_signature(db_session, d, agent="onboarding")
        assert found is not None

        not_found = await check_signature(
            db_session, d, agent="pr_reviewer"
        )
        assert not_found is None

    async def test_check_normalizes_repo_case(
        self, db_session: AsyncSession
    ):
        """Recording with 'Owner/Repo' and checking 'owner/repo' must match."""
        record_d = _decision(
            "create_issue",
            issue=None,
            repo="Owner/Repo",
            payload={"title": "Fix bug"},
        )
        check_d = _decision(
            "create_issue",
            issue=None,
            repo="owner/repo",
            payload={"title": "Fix bug"},
        )
        await record_action(
            db_session, record_d, agent="onboarding", outcome="executed"
        )
        await db_session.commit()

        found = await check_signature(db_session, check_d, agent="onboarding")
        assert found is not None

    async def test_different_repo_not_deduped(
        self, db_session: AsyncSession
    ):
        a = _decision(
            "create_issue",
            issue=None,
            repo="owner/repo",
            payload={"title": "Fix bug"},
        )
        b = _decision(
            "create_issue",
            issue=None,
            repo="other/repo",
            payload={"title": "Fix bug"},
        )
        await record_action(
            db_session, a, agent="onboarding", outcome="executed"
        )
        await db_session.commit()

        found = await check_signature(db_session, b, agent="onboarding")
        assert found is None


class TestUniqueConstraint:
    async def test_second_record_raises_integrity_error(
        self, db_session: AsyncSession
    ):
        """The same signature can only be inserted once. Day 2 uses this as
        the concurrent-race safety net."""
        d = _decision(
            "create_issue", issue=None, payload={"title": "Fix bug"}
        )
        await record_action(
            db_session, d, agent="onboarding", outcome="executed"
        )
        await db_session.commit()

        with pytest.raises(IntegrityError):
            await record_action(
                db_session, d, agent="onboarding", outcome="executed"
            )
            await db_session.commit()

        # Clean up the broken transaction so the test fixture's TRUNCATE works.
        await db_session.rollback()


class TestRecordPersistsEvidence:
    async def test_evidence_roundtrips_as_list(
        self, db_session: AsyncSession
    ):
        d = _decision(
            "create_issue",
            issue=None,
            payload={"title": "x"},
            evidence=["e1", "e2", "e3"],
        )
        recorded = await record_action(
            db_session, d, agent="onboarding", outcome="executed"
        )
        await db_session.commit()

        # Reload from DB to confirm the JSONB column deserializes as a list.
        fresh = (
            await db_session.execute(
                select(AgentAction).where(AgentAction.id == recorded.id)
            )
        ).scalar_one()
        assert fresh.evidence == ["e1", "e2", "e3"]
