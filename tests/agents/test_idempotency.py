"""Idempotency of side effects across concurrent runs, failures and modes.

These use real Postgres transactions: the guarantee depends on the
unique index on agent_actions, not on application-level checks.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from gita.agents.decisions import (
    DEFAULT_THRESHOLDS,
    Decision,
    Outcome,
    WriteMode,
    execute_decision,
)
from gita.agents.dedupe import SIGNATURE_SHAPES
from gita.db.models import AgentAction
from gita.github.client import SUPPORTED_ACTIONS


def _review(key: str = "pr-review:7:abc") -> Decision:
    return Decision(
        action="comment",
        target={"repo": "acme/app", "issue": 7},
        payload={"body": "review"},
        confidence=0.9,
        idempotency_key=key,
    )


class _SlowClient:
    """Holds the first call open until released, to force an overlap."""

    def __init__(self) -> None:
        self.calls: list[Decision] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, decision: Decision) -> dict[str, Any]:
        self.calls.append(decision)
        self.entered.set()
        await self.release.wait()
        return {"comment_id": len(self.calls)}


class _FailingClient:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, decision: Decision) -> dict[str, Any]:
        self.calls += 1
        raise RuntimeError("GitHub 502")


async def _count_rows(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(AgentAction))).scalar_one()


class TestConcurrentRuns:
    async def test_two_workers_post_once(self, test_engine: AsyncEngine, db_session):
        """Both runs pass the pre-check; only the reservation decides.

        Worker A reserves and is mid-call to GitHub when worker B starts.
        B's insert blocks on A's uncommitted row, then fails once A
        commits, so B never calls GitHub.
        """
        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        client = _SlowClient()

        async def run(session: AsyncSession):
            result = await execute_decision(
                _review(), mode=WriteMode.FULL, client=client, session=session, agent="pr_reviewer"
            )
            await session.commit()
            return result

        async with factory() as s_a, factory() as s_b:
            task_a = asyncio.create_task(run(s_a))
            await client.entered.wait()  # A holds the reservation
            task_b = asyncio.create_task(run(s_b))
            await asyncio.sleep(0.3)  # B is now blocked on the unique index
            client.release.set()
            result_a, result_b = await asyncio.gather(task_a, task_b)

        assert len(client.calls) == 1
        assert {result_a.outcome, result_b.outcome} == {Outcome.EXECUTED, Outcome.DEDUPED}
        assert await _count_rows(db_session) == 1


class TestFailureReleasesReservation:
    async def test_failed_call_can_be_retried(self, db_session: AsyncSession):
        failing = _FailingClient()
        first = await execute_decision(
            _review(), mode=WriteMode.FULL, client=failing, session=db_session, agent="pr_reviewer"
        )
        assert first.outcome == Outcome.ERROR
        assert await _count_rows(db_session) == 0  # reservation released

        ok = _SlowClient()
        ok.release.set()
        retry = await execute_decision(
            _review(), mode=WriteMode.FULL, client=ok, session=db_session, agent="pr_reviewer"
        )
        assert retry.outcome == Outcome.EXECUTED
        row = (await db_session.execute(select(AgentAction))).scalar_one()
        assert row.outcome == "executed" and row.external_id == "1" and row.live is True


class TestShadowThenLive:
    async def test_shadow_observation_does_not_suppress_live_write(self, db_session: AsyncSession):
        shadow = await execute_decision(
            _review(), mode=WriteMode.SHADOW, session=db_session, agent="pr_reviewer"
        )
        assert shadow.outcome == Outcome.SHADOW_LOGGED

        again = await execute_decision(
            _review(), mode=WriteMode.SHADOW, session=db_session, agent="pr_reviewer"
        )
        assert again.outcome == Outcome.DEDUPED  # shadow still dedupes shadow

        client = _SlowClient()
        client.release.set()
        live = await execute_decision(
            _review(),
            mode=WriteMode.COMMENT,
            client=client,
            session=db_session,
            agent="pr_reviewer",
        )
        assert live.outcome == Outcome.EXECUTED
        assert len(client.calls) == 1

        replay = await execute_decision(
            _review(), mode=WriteMode.FULL, client=client, session=db_session, agent="pr_reviewer"
        )
        assert replay.outcome == Outcome.DEDUPED
        assert len(client.calls) == 1


class TestChainInOneSession:
    async def test_later_dedupe_does_not_roll_back_earlier_rows(self, db_session: AsyncSession):
        """The old race path called session.rollback(), discarding rows the
        same session had recorded earlier in a decision chain."""
        client = _SlowClient()
        client.release.set()
        first = await execute_decision(
            _review("a"), mode=WriteMode.FULL, client=client, session=db_session, agent="x"
        )
        dup = await execute_decision(
            _review("a"), mode=WriteMode.FULL, client=client, session=db_session, agent="x"
        )
        second = await execute_decision(
            _review("b"), mode=WriteMode.FULL, client=client, session=db_session, agent="x"
        )
        assert [first.outcome, dup.outcome, second.outcome] == [
            Outcome.EXECUTED,
            Outcome.DEDUPED,
            Outcome.EXECUTED,
        ]
        assert await _count_rows(db_session) == 2


def test_action_vocabulary_is_consistent():
    """Thresholds, signatures and the client used to disagree ("label" vs
    "add_label"/"remove_label"), so every label decision raised."""
    assert set(DEFAULT_THRESHOLDS) == set(SIGNATURE_SHAPES) == set(SUPPORTED_ACTIONS)
