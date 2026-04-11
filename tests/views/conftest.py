"""Fixtures shared across the view tests.

``indexed_synth_py`` runs the real ingest pipeline against
``tests/fixtures/synthetic_py`` and yields the ``(session, repo_name)`` pair
so each test can query the populated tables.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from gita.indexer.ingest import index_repository

SYNTH_REPO = (
    Path(__file__).parent.parent / "fixtures" / "synthetic_py"
).resolve()
SYNTH_REPO_NAME = "synthetic_py"


@pytest_asyncio.fixture
async def indexed_synth_py(
    db_session: AsyncSession,
) -> AsyncIterator[tuple[AsyncSession, str]]:
    """Ingest synthetic_py, commit, and yield (session, repo_name).

    The outer ``db_session`` fixture truncates the three tables after the
    test, so each view test starts and ends with a clean slate.
    """
    await index_repository(db_session, SYNTH_REPO_NAME, SYNTH_REPO)
    await db_session.commit()
    yield db_session, SYNTH_REPO_NAME
