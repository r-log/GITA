"""Golden-agent test for a local AMASS clone.

Two skip paths:
1. Skipped at collection if ``gita.agents.onboarding`` doesn't exist (Day 5).
2. Skipped at runtime if the AMASS checkout isn't on disk at the expected
   path (``GITA_AMASS_PATH`` env var or the hardcoded default).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

run_onboarding = pytest.importorskip(
    "gita.agents.onboarding", reason="onboarding agent lands on Day 5"
).run_onboarding

from gita.indexer.ingest import index_repository  # noqa: E402
from tests.golden_agents.checklist import check_output  # noqa: E402
from tests.golden_agents.checklists.amass import CHECKLIST  # noqa: E402

_DEFAULT_AMASS_PATH = Path(
    "C:/Users/Roko/Documents/PYTHON/AMAS/electrician-log-mvp"
)
AMASS_PATH = Path(os.environ.get("GITA_AMASS_PATH", str(_DEFAULT_AMASS_PATH)))


@pytest.mark.skipif(
    not AMASS_PATH.is_dir(),
    reason=f"AMASS not available at {AMASS_PATH}",
)
async def test_amass_checklist_passes(db_session, real_llm):
    await index_repository(db_session, "amass", AMASS_PATH)
    await db_session.commit()

    result = await run_onboarding(db_session, "amass", llm=real_llm)

    violations = check_output(result, CHECKLIST)
    assert violations == [], (
        "amass checklist violations:\n  - " + "\n  - ".join(violations)
    )
