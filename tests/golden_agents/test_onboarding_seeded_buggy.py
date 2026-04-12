"""Golden-agent test for the seeded_buggy fixture.

Skipped at collection time until ``gita.agents.onboarding`` lands on Day 5.
"""
from __future__ import annotations

from pathlib import Path

import pytest

run_onboarding = pytest.importorskip(
    "gita.agents.onboarding", reason="onboarding agent lands on Day 5"
).run_onboarding

from gita.indexer.ingest import index_repository  # noqa: E402
from tests.golden_agents.checklist import check_output  # noqa: E402
from tests.golden_agents.checklists.seeded_buggy import (  # noqa: E402
    CHECKLIST,
)

FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "seeded_buggy"
).resolve()


async def test_seeded_buggy_checklist_passes(db_session, real_llm):
    assert FIXTURE.is_dir(), f"fixture missing at {FIXTURE}"

    await index_repository(db_session, "seeded_buggy", FIXTURE)
    await db_session.commit()

    result = await run_onboarding(db_session, "seeded_buggy", llm=real_llm)

    violations = check_output(result, CHECKLIST)
    assert violations == [], (
        "seeded_buggy checklist violations:\n  - " + "\n  - ".join(violations)
    )
