"""Issue-flow golden test — the Week 3 Day 5 acceptance bar.

This is the integration test that proves the Week 3 contract chain works
end-to-end without touching real GitHub:

    real_llm → run_onboarding → build_onboarding_issue_decisions
        → execute_decision (under each WriteMode) → FakeActionClient

The four assertions:
- ``WRITE_MODE=shadow`` → every Decision is ``SHADOW_LOGGED`` (no client
  call) and persisted to ``agent_actions`` for dedupe.
- Second ``WRITE_MODE=shadow`` run → every Decision is ``DEDUPED`` (nothing
  sent to the client, the session recognized each signature from run 1).
- ``WRITE_MODE=comment`` + a fallback landing issue → every Decision is
  ``DOWNGRADED_WRITE_MODE`` and the client receives ``action="comment"``
  posts at the fallback target.
- ``WRITE_MODE=full`` against a mock client → every Decision is
  ``EXECUTED``.

The test is gated behind ``GITA_RUN_LLM_TESTS=1`` because
``run_onboarding`` calls the real OpenRouter API. Cost: ~$0.10 per run.
It uses its own ``db_session`` and indexes the fixture inline so it's
self-contained — running it alone does not require other golden tests.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

run_onboarding = pytest.importorskip(
    "gita.agents.onboarding", reason="onboarding agent lands on Day 5"
).run_onboarding

from gita.agents.decisions import (  # noqa: E402
    ActionClient,
    Decision,
    Outcome,
    WriteMode,
    execute_decision,
)
from gita.agents.onboarding import (  # noqa: E402
    build_onboarding_issue_decisions,
)
from gita.indexer.ingest import index_repository  # noqa: E402

FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "seeded_buggy"
).resolve()

TARGET_REPO = "r-log/throwaway-dev"
FALLBACK_ISSUE = 9999


class _RecordingClient:
    """Mock ActionClient that captures every execute() call.

    Week 3 Day 3's real ``GithubClient`` is not used here — this test
    proves the gate layer + bridge layer talk to *any* ActionClient-shape
    correctly, without touching the network. The Day 7 live flip has its
    own separate proof against the real GithubClient.
    """

    def __init__(self) -> None:
        self.calls: list[Decision] = []

    async def execute(self, decision: Decision) -> dict[str, Any]:
        self.calls.append(decision)
        # Mimic the real client's response shape for each action so the
        # side_effect threading in execute_decision finds an external_id.
        if decision.action == "create_issue":
            title = decision.payload.get("title", "")
            issue_num = 1000 + len(self.calls)
            return {
                "kind": "issue",
                "id": issue_num,
                "html_url": (
                    f"https://github.com/{TARGET_REPO}/issues/{issue_num}"
                ),
                "title": title,
            }
        if decision.action == "comment":
            comment_id = 2000 + len(self.calls)
            return {
                "kind": "comment",
                "id": comment_id,
                "html_url": (
                    f"https://github.com/{TARGET_REPO}/issues/"
                    f"{FALLBACK_ISSUE}#issuecomment-{comment_id}"
                ),
            }
        return {"ok": True}


@pytest.fixture
def recording_client() -> _RecordingClient:
    return _RecordingClient()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------
async def test_onboarding_issue_flow_across_write_modes(
    db_session, real_llm, recording_client: _RecordingClient
):
    """End-to-end: onboarding → bridge → gate → mock client under each mode.

    Structural typing: the mock satisfies the ActionClient protocol even
    though it's not a GithubClient. This is the whole point of Day 1's
    Protocol-based framework — the gate doesn't care which client lands.
    """
    assert FIXTURE.is_dir(), f"fixture missing at {FIXTURE}"
    client: ActionClient = recording_client  # structural typing check

    # ------------------------------------------------------------------
    # Set up: index the fixture, run the real LLM onboarding once, and
    # build the issue decisions we'll route through every mode.
    # ------------------------------------------------------------------
    await index_repository(db_session, "seeded_buggy", FIXTURE)
    await db_session.commit()

    result = await run_onboarding(db_session, "seeded_buggy", llm=real_llm)
    assert result.milestones, (
        "expected at least one milestone from seeded_buggy — the fixture has "
        "9 planted bugs. If this fails, something's wrong with the agent "
        "pipeline, not this test."
    )

    decisions = build_onboarding_issue_decisions(
        result,
        target_repo=TARGET_REPO,
        fallback_comment_target=FALLBACK_ISSUE,
    )
    assert len(decisions) == len(result.milestones)

    # ------------------------------------------------------------------
    # 1. SHADOW mode → all SHADOW_LOGGED, no client calls, rows recorded.
    # ------------------------------------------------------------------
    for decision in decisions:
        shadow_result = await execute_decision(
            decision,
            mode=WriteMode.SHADOW,
            client=client,
            session=db_session,
            agent="onboarding",
        )
        assert shadow_result.outcome == Outcome.SHADOW_LOGGED
    assert recording_client.calls == []
    await db_session.commit()

    # ------------------------------------------------------------------
    # 2. Second SHADOW pass → all DEDUPED, still no client calls.
    # ------------------------------------------------------------------
    for decision in decisions:
        dedupe_result = await execute_decision(
            decision,
            mode=WriteMode.SHADOW,
            client=client,
            session=db_session,
            agent="onboarding",
        )
        assert dedupe_result.outcome == Outcome.DEDUPED, (
            f"expected DEDUPED on second shadow pass, got "
            f"{dedupe_result.outcome.value}"
        )
    assert recording_client.calls == []
    await db_session.commit()

    # ------------------------------------------------------------------
    # 3. COMMENT mode with a fresh agent scope → all DOWNGRADED to comment
    #    at the fallback issue. Downgrade comments must reach the client.
    # ------------------------------------------------------------------
    for decision in decisions:
        comment_result = await execute_decision(
            decision,
            mode=WriteMode.COMMENT,
            client=client,
            session=db_session,
            agent="onboarding_comment_phase",
        )
        assert comment_result.outcome == Outcome.DOWNGRADED_WRITE_MODE
        assert comment_result.executed is True
    assert len(recording_client.calls) == len(decisions)
    for call in recording_client.calls:
        assert call.action == "comment"
        assert call.target["issue"] == FALLBACK_ISSUE
        assert call.target["repo"] == TARGET_REPO
        # The downgrade body must mention the original intended action.
        assert "create_issue" in call.payload["body"]
    await db_session.commit()

    # ------------------------------------------------------------------
    # 4. FULL mode with another fresh agent scope → all EXECUTED as
    #    create_issue against the mock client.
    # ------------------------------------------------------------------
    recording_client.calls.clear()
    for decision in decisions:
        full_result = await execute_decision(
            decision,
            mode=WriteMode.FULL,
            client=client,
            session=db_session,
            agent="onboarding_full_phase",
        )
        assert full_result.outcome == Outcome.EXECUTED
        assert full_result.executed is True
        side = full_result.side_effect or {}
        assert side.get("kind") == "issue"
        assert "html_url" in side
    assert len(recording_client.calls) == len(decisions)
    for call in recording_client.calls:
        assert call.action == "create_issue"
        assert call.target["repo"] == TARGET_REPO
        assert "title" in call.payload
    await db_session.commit()
