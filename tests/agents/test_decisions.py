"""Tests for the confidence-gated write framework.

Covers:
- Decision validation (confidence range, empty action)
- Threshold lookup (known + unknown actions)
- Shadow mode: never executes, always logs
- Comment mode: executes comments, downgrades everything else
- Full mode: executes anything that passes its threshold
- Low-confidence actions downgrade to comments across all modes
- Missing client in non-shadow mode fails cleanly
- Client exceptions produce ERROR outcome
- Downgrade comment body includes evidence chain
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.decisions import (
    DEFAULT_THRESHOLDS,
    ActionClient,
    Decision,
    DecisionResult,
    Outcome,
    WriteMode,
    execute_decision,
    get_threshold,
)
from gita.db.models import AgentAction


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
class FakeActionClient:
    """Captures every execute() call for assertion."""

    def __init__(self, should_raise: bool = False):
        self.calls: list[Decision] = []
        self.should_raise = should_raise

    async def execute(self, decision: Decision) -> dict[str, Any]:
        self.calls.append(decision)
        if self.should_raise:
            raise RuntimeError("simulated client failure")
        return {"ok": True, "action": decision.action}


def _make_decision(
    action: str = "comment",
    confidence: float = 0.9,
    evidence: list[str] | None = None,
) -> Decision:
    return Decision(
        action=action,
        target={"repo": "owner/repo", "issue": 42},
        payload={"body": "hello"},
        evidence=evidence or ["evidence one", "evidence two"],
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Decision validation
# ---------------------------------------------------------------------------
class TestDecisionValidation:
    def test_valid_decision(self):
        d = Decision(
            action="comment",
            target={"repo": "a/b"},
            payload={},
            confidence=0.5,
        )
        assert d.action == "comment"
        assert d.confidence == 0.5

    def test_empty_action_raises(self):
        with pytest.raises(ValueError, match="action cannot be empty"):
            Decision(action="", target={}, payload={}, confidence=0.5)

    def test_negative_confidence_raises(self):
        with pytest.raises(ValueError, match="confidence must be in"):
            Decision(action="comment", target={}, payload={}, confidence=-0.1)

    def test_confidence_above_one_raises(self):
        with pytest.raises(ValueError, match="confidence must be in"):
            Decision(action="comment", target={}, payload={}, confidence=1.1)

    def test_zero_and_one_are_valid(self):
        Decision(action="comment", target={}, payload={}, confidence=0.0)
        Decision(action="comment", target={}, payload={}, confidence=1.0)


# ---------------------------------------------------------------------------
# Threshold lookup
# ---------------------------------------------------------------------------
class TestThresholds:
    def test_known_action_returns_default(self):
        assert get_threshold("comment") == DEFAULT_THRESHOLDS["comment"]
        assert get_threshold("create_issue") == DEFAULT_THRESHOLDS["create_issue"]

    def test_unknown_action_raises(self):
        with pytest.raises(KeyError, match="no threshold configured"):
            get_threshold("unknown_action")

    def test_custom_thresholds_override(self):
        custom = {"comment": 0.99}
        assert get_threshold("comment", custom) == 0.99

    def test_custom_thresholds_still_raise_unknown(self):
        with pytest.raises(KeyError):
            get_threshold("create_issue", {"comment": 0.3})


# ---------------------------------------------------------------------------
# Shadow mode: never executes, always logs
# ---------------------------------------------------------------------------
class TestShadowMode:
    async def test_shadow_never_calls_client(self):
        client = FakeActionClient()
        decision = _make_decision(action="comment", confidence=0.9)

        result = await execute_decision(
            decision, mode=WriteMode.SHADOW, client=client
        )

        assert result.outcome == Outcome.SHADOW_LOGGED
        assert result.executed is False
        assert client.calls == []

    async def test_shadow_with_no_client_is_fine(self):
        decision = _make_decision(action="comment", confidence=0.9)
        result = await execute_decision(decision, mode=WriteMode.SHADOW)
        assert result.outcome == Outcome.SHADOW_LOGGED
        assert result.executed is False
        assert result.error is None

    async def test_shadow_still_downgrades_low_confidence(self):
        """Shadow mode still routes through the threshold check so the outcome
        reflects the downgrade, even though nothing executes."""
        decision = _make_decision(action="create_issue", confidence=0.4)
        result = await execute_decision(decision, mode=WriteMode.SHADOW)
        assert result.outcome == Outcome.DOWNGRADED_LOW_CONFIDENCE
        assert result.executed is False

    async def test_shadow_still_downgrades_wrong_mode(self):
        """Shadow mode doesn't change outcome classification for other gates."""
        # This case is "would be downgraded in comment mode, but we're in
        # shadow, so the write-mode downgrade rule doesn't apply."
        decision = _make_decision(action="create_issue", confidence=0.9)
        result = await execute_decision(decision, mode=WriteMode.SHADOW)
        assert result.outcome == Outcome.SHADOW_LOGGED


# ---------------------------------------------------------------------------
# Full mode: executes anything passing its threshold
# ---------------------------------------------------------------------------
class TestFullMode:
    async def test_high_confidence_comment_executes(self):
        client = FakeActionClient()
        decision = _make_decision(action="comment", confidence=0.9)
        result = await execute_decision(
            decision, mode=WriteMode.FULL, client=client
        )
        assert result.outcome == Outcome.EXECUTED
        assert result.executed is True
        assert len(client.calls) == 1
        assert client.calls[0].action == "comment"

    async def test_high_confidence_create_issue_executes(self):
        client = FakeActionClient()
        decision = _make_decision(action="create_issue", confidence=0.9)
        result = await execute_decision(
            decision, mode=WriteMode.FULL, client=client
        )
        assert result.outcome == Outcome.EXECUTED
        assert client.calls[0].action == "create_issue"

    async def test_full_mode_no_client_rejects(self):
        decision = _make_decision(action="comment", confidence=0.9)
        result = await execute_decision(decision, mode=WriteMode.FULL)
        assert result.outcome == Outcome.REJECTED_NO_CLIENT
        assert result.executed is False
        assert "no ActionClient" in (result.error or "")

    async def test_full_mode_client_raises_becomes_error(self):
        client = FakeActionClient(should_raise=True)
        decision = _make_decision(action="comment", confidence=0.9)
        result = await execute_decision(
            decision, mode=WriteMode.FULL, client=client
        )
        assert result.outcome == Outcome.ERROR
        assert result.executed is False
        assert "simulated client failure" in (result.error or "")


# ---------------------------------------------------------------------------
# Comment mode: executes comments, downgrades everything else
# ---------------------------------------------------------------------------
class TestCommentMode:
    async def test_comment_action_executes(self):
        client = FakeActionClient()
        decision = _make_decision(action="comment", confidence=0.9)
        result = await execute_decision(
            decision, mode=WriteMode.COMMENT, client=client
        )
        assert result.outcome == Outcome.EXECUTED
        assert result.executed is True
        assert len(client.calls) == 1
        assert client.calls[0].action == "comment"

    async def test_create_issue_downgrades_to_comment(self):
        client = FakeActionClient()
        decision = _make_decision(action="create_issue", confidence=0.9)
        result = await execute_decision(
            decision, mode=WriteMode.COMMENT, client=client
        )
        assert result.outcome == Outcome.DOWNGRADED_WRITE_MODE
        assert result.executed is True  # the comment was posted
        assert len(client.calls) == 1
        # The client received a *comment* action, not the original create_issue
        assert client.calls[0].action == "comment"
        assert "Intended action" in client.calls[0].payload["body"]
        assert "create_issue" in client.calls[0].payload["body"]

    async def test_close_issue_downgrades_to_comment(self):
        client = FakeActionClient()
        decision = _make_decision(action="close_issue", confidence=0.95)
        result = await execute_decision(
            decision, mode=WriteMode.COMMENT, client=client
        )
        assert result.outcome == Outcome.DOWNGRADED_WRITE_MODE
        assert client.calls[0].action == "comment"


# ---------------------------------------------------------------------------
# Low confidence: always downgrades regardless of mode
# ---------------------------------------------------------------------------
class TestLowConfidenceDowngrade:
    async def test_low_confidence_create_issue_in_full_mode(self):
        client = FakeActionClient()
        # create_issue threshold is 0.7; 0.4 is below
        decision = _make_decision(action="create_issue", confidence=0.4)
        result = await execute_decision(
            decision, mode=WriteMode.FULL, client=client
        )
        assert result.outcome == Outcome.DOWNGRADED_LOW_CONFIDENCE
        assert result.executed is True
        assert client.calls[0].action == "comment"
        assert "confidence" in (result.downgrade_reason or "").lower()

    async def test_low_confidence_comment_in_full_mode(self):
        client = FakeActionClient()
        # comment threshold is 0.3; 0.1 is below
        decision = _make_decision(action="comment", confidence=0.1)
        result = await execute_decision(
            decision, mode=WriteMode.FULL, client=client
        )
        assert result.outcome == Outcome.DOWNGRADED_LOW_CONFIDENCE
        assert client.calls[0].action == "comment"

    async def test_low_confidence_in_comment_mode(self):
        client = FakeActionClient()
        decision = _make_decision(action="close_issue", confidence=0.2)
        result = await execute_decision(
            decision, mode=WriteMode.COMMENT, client=client
        )
        # Low confidence wins over write-mode downgrade
        assert result.outcome == Outcome.DOWNGRADED_LOW_CONFIDENCE
        assert client.calls[0].action == "comment"


# ---------------------------------------------------------------------------
# Downgrade message body includes evidence
# ---------------------------------------------------------------------------
class TestDowngradeBody:
    async def test_body_has_intended_action(self):
        client = FakeActionClient()
        decision = _make_decision(
            action="create_issue",
            confidence=0.4,
            evidence=["commit abc says 'fixes #1'", "all tests passing"],
        )
        await execute_decision(
            decision, mode=WriteMode.FULL, client=client
        )
        body = client.calls[0].payload["body"]
        assert "Intended action" in body
        assert "create_issue" in body
        assert "commit abc says 'fixes #1'" in body
        assert "all tests passing" in body
        assert "0.40" in body  # confidence shown

    async def test_body_has_downgrade_reason(self):
        client = FakeActionClient()
        decision = _make_decision(action="create_issue", confidence=0.4)
        await execute_decision(
            decision, mode=WriteMode.FULL, client=client
        )
        body = client.calls[0].payload["body"]
        assert "below threshold" in body.lower()


# ---------------------------------------------------------------------------
# Unknown action fails loudly
# ---------------------------------------------------------------------------
class TestUnknownAction:
    async def test_unknown_action_raises_keyerror(self):
        decision = Decision(
            action="teleport",
            target={},
            payload={},
            confidence=0.9,
        )
        with pytest.raises(KeyError, match="no threshold configured"):
            await execute_decision(decision, mode=WriteMode.SHADOW)


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------
class TestProtocolCompliance:
    def test_fake_client_satisfies_protocol(self):
        """Structural typing check — FakeActionClient should be an ActionClient."""
        client: ActionClient = FakeActionClient()
        assert callable(client.execute)


# ---------------------------------------------------------------------------
# Dedupe integration — pre-gate + post-record plumbing with a real DB session.
# ---------------------------------------------------------------------------
async def _count_rows(db_session: AsyncSession) -> int:
    result = await db_session.execute(select(func.count()).select_from(AgentAction))
    return result.scalar_one()


class TestDedupeValidation:
    async def test_session_without_agent_raises(self, db_session: AsyncSession):
        decision = _make_decision(action="comment", confidence=0.9)
        with pytest.raises(ValueError, match="'agent' is required"):
            await execute_decision(
                decision,
                mode=WriteMode.SHADOW,
                session=db_session,
            )

    async def test_session_with_empty_agent_raises(self, db_session: AsyncSession):
        decision = _make_decision(action="comment", confidence=0.9)
        with pytest.raises(ValueError, match="'agent' is required"):
            await execute_decision(
                decision,
                mode=WriteMode.SHADOW,
                session=db_session,
                agent="",
            )

    async def test_no_session_works_without_agent(self):
        """Backward-compat: old call shape (no session, no agent) still works."""
        decision = _make_decision(action="comment", confidence=0.9)
        result = await execute_decision(decision, mode=WriteMode.SHADOW)
        assert result.outcome == Outcome.SHADOW_LOGGED


class TestDedupeShadowMode:
    async def test_shadow_first_run_records(self, db_session: AsyncSession):
        decision = _make_decision(action="comment", confidence=0.9)
        result = await execute_decision(
            decision,
            mode=WriteMode.SHADOW,
            session=db_session,
            agent="onboarding",
        )
        await db_session.commit()

        assert result.outcome == Outcome.SHADOW_LOGGED
        assert result.side_effect is not None
        assert "agent_action_id" in result.side_effect
        assert await _count_rows(db_session) == 1

    async def test_shadow_second_run_dedupes(self, db_session: AsyncSession):
        decision = _make_decision(action="comment", confidence=0.9)
        first = await execute_decision(
            decision,
            mode=WriteMode.SHADOW,
            session=db_session,
            agent="onboarding",
        )
        await db_session.commit()
        second = await execute_decision(
            decision,
            mode=WriteMode.SHADOW,
            session=db_session,
            agent="onboarding",
        )
        await db_session.commit()

        assert first.outcome == Outcome.SHADOW_LOGGED
        assert second.outcome == Outcome.DEDUPED
        assert second.side_effect is not None
        assert second.side_effect["previous_outcome"] == "shadow_logged"
        assert await _count_rows(db_session) == 1


class TestDedupeFullMode:
    async def test_full_mode_first_run_executes_and_records(
        self, db_session: AsyncSession
    ):
        client = FakeActionClient()
        decision = _make_decision(action="comment", confidence=0.9)
        result = await execute_decision(
            decision,
            mode=WriteMode.FULL,
            client=client,
            session=db_session,
            agent="onboarding",
        )
        await db_session.commit()

        assert result.outcome == Outcome.EXECUTED
        assert len(client.calls) == 1
        assert await _count_rows(db_session) == 1

    async def test_full_mode_second_run_dedupes_and_skips_client(
        self, db_session: AsyncSession
    ):
        client = FakeActionClient()
        decision = _make_decision(action="comment", confidence=0.9)
        await execute_decision(
            decision,
            mode=WriteMode.FULL,
            client=client,
            session=db_session,
            agent="onboarding",
        )
        await db_session.commit()

        second = await execute_decision(
            decision,
            mode=WriteMode.FULL,
            client=client,
            session=db_session,
            agent="onboarding",
        )

        assert second.outcome == Outcome.DEDUPED
        # Client was only called on the first run.
        assert len(client.calls) == 1

    async def test_error_outcome_not_recorded(self, db_session: AsyncSession):
        """Client-side failure must not dedupe — retries should still execute."""
        client = FakeActionClient(should_raise=True)
        decision = _make_decision(action="comment", confidence=0.9)
        result = await execute_decision(
            decision,
            mode=WriteMode.FULL,
            client=client,
            session=db_session,
            agent="onboarding",
        )
        await db_session.commit()

        assert result.outcome == Outcome.ERROR
        assert await _count_rows(db_session) == 0

    async def test_rejected_no_client_not_recorded(self, db_session: AsyncSession):
        decision = _make_decision(action="comment", confidence=0.9)
        result = await execute_decision(
            decision,
            mode=WriteMode.FULL,
            session=db_session,
            agent="onboarding",
        )
        await db_session.commit()

        assert result.outcome == Outcome.REJECTED_NO_CLIENT
        assert await _count_rows(db_session) == 0


class TestDedupeCommentModeDowngrade:
    async def test_create_issue_downgrade_records_and_dedupes(
        self, db_session: AsyncSession
    ):
        """Week 3 acceptance regression: a downgraded create_issue in comment
        mode must dedupe against itself on a second run."""
        client = FakeActionClient()
        decision = Decision(
            action="create_issue",
            target={"repo": "owner/repo", "issue": 42},
            payload={"title": "Fix SQL injection", "body": "details"},
            evidence=["e1"],
            confidence=0.9,
        )

        first = await execute_decision(
            decision,
            mode=WriteMode.COMMENT,
            client=client,
            session=db_session,
            agent="onboarding",
        )
        await db_session.commit()

        second = await execute_decision(
            decision,
            mode=WriteMode.COMMENT,
            client=client,
            session=db_session,
            agent="onboarding",
        )
        await db_session.commit()

        # First run posted a downgrade explanation comment.
        assert first.outcome == Outcome.DOWNGRADED_WRITE_MODE
        assert first.executed is True
        # Second run deduped — client only saw the first run.
        assert second.outcome == Outcome.DEDUPED
        assert len(client.calls) == 1
        assert client.calls[0].action == "comment"  # downgraded
        assert await _count_rows(db_session) == 1


class TestDedupeAgentScoping:
    async def test_different_agent_does_not_dedupe(
        self, db_session: AsyncSession
    ):
        client = FakeActionClient()
        decision = _make_decision(action="comment", confidence=0.9)

        await execute_decision(
            decision,
            mode=WriteMode.FULL,
            client=client,
            session=db_session,
            agent="onboarding",
        )
        await db_session.commit()

        other = await execute_decision(
            decision,
            mode=WriteMode.FULL,
            client=client,
            session=db_session,
            agent="pr_reviewer",
        )
        await db_session.commit()

        assert other.outcome == Outcome.EXECUTED
        assert len(client.calls) == 2
        assert await _count_rows(db_session) == 2


class TestDedupedResultShape:
    async def test_deduped_result_carries_previous_external_id(
        self, db_session: AsyncSession
    ):
        class IdentifyingClient(FakeActionClient):
            async def execute(self, decision: Decision) -> dict[str, Any]:
                self.calls.append(decision)
                return {"ok": True, "id": "gh_comment_98765"}

        client = IdentifyingClient()
        decision = _make_decision(action="comment", confidence=0.9)

        first = await execute_decision(
            decision,
            mode=WriteMode.FULL,
            client=client,
            session=db_session,
            agent="onboarding",
        )
        await db_session.commit()
        assert first.side_effect is not None
        assert first.side_effect.get("id") == "gh_comment_98765"

        second = await execute_decision(
            decision,
            mode=WriteMode.FULL,
            client=client,
            session=db_session,
            agent="onboarding",
        )
        assert second.outcome == Outcome.DEDUPED
        assert second.side_effect is not None
        assert second.side_effect["external_id"] == "gh_comment_98765"
        assert second.side_effect["previous_outcome"] == "executed"
