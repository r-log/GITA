"""Golden-agent test for the flask_starter fixture.

Skipped at collection time until ``gita.agents.onboarding`` lands on Day 5.
Once the agent module exists, pytest will start executing this file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# If the onboarding agent doesn't exist yet, skip the whole module.
run_onboarding = pytest.importorskip(
    "gita.agents.onboarding", reason="onboarding agent lands on Day 5"
).run_onboarding

from gita.indexer.ingest import index_repository  # noqa: E402
from tests.golden_agents.checklist import check_output  # noqa: E402
from tests.golden_agents.checklists.flask_starter import (  # noqa: E402
    CHECKLIST,
)

FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "flask_starter"
).resolve()


async def test_flask_starter_checklist_passes(db_session, real_llm):
    assert FIXTURE.is_dir(), f"fixture missing at {FIXTURE}"

    # 1. Index the fixture into the test DB.
    await index_repository(db_session, "flask_starter", FIXTURE)
    await db_session.commit()

    # 2. Run the onboarding agent with the real OpenRouter LLM.
    result = await run_onboarding(db_session, "flask_starter", llm=real_llm)

    # 3. Check the output against the fixture's checklist.
    violations = check_output(result, CHECKLIST)
    assert violations == [], (
        "flask_starter checklist violations:\n  - " + "\n  - ".join(violations)
    )
