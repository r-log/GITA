"""Shared pytest fixtures.

Three tricky things this module handles:

1. **Do not collect fake Python files under ``tests/fixtures/``** as tests.

2. **Windows + pytest-asyncio + asyncpg event loop policy.** Sets the Proactor
   event loop policy at module import time, before anything else touches
   asyncio. asyncpg on Windows requires Proactor.

3. **Bootstrap the test database at module load time** — before any pytest
   plugin code runs. At that point no event loop exists yet, ``asyncio.run``
   works cleanly, and SQLAlchemy/asyncpg aren't locked to a foreign loop.

Speed: both the engine and the event loop are session-scoped, so we create
the ``github_assistant_test`` DB once per session, open one engine once per
session, and reuse connections across tests. Each test just gets a fresh
``AsyncSession`` and a ``TRUNCATE`` at the end.
"""
from __future__ import annotations

import asyncio
import sys

# FIRST — before any asyncio work anywhere in this module or its imports.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from collections.abc import AsyncIterator  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from gita.config import settings  # noqa: E402
from gita.db.models import Base  # noqa: E402

# Do not collect the fake Python files inside tests/fixtures/ as pytest tests.
collect_ignore_glob = ["fixtures/**"]

TEST_DB_NAME = "github_assistant_test"
_TEST_DB_URL = settings.database_url.rsplit("/", 1)[0] + f"/{TEST_DB_NAME}"
_ADMIN_DB_URL = settings.database_url.rsplit("/", 1)[0] + "/postgres"


async def _bootstrap_test_db() -> None:
    """Drop + recreate the test database, create all tables."""
    admin = create_async_engine(
        _ADMIN_DB_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin.connect() as conn:
        await conn.execute(
            text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')
        )
        await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    await admin.dispose()

    engine = create_async_engine(_TEST_DB_URL, echo=False, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


# Module-level bootstrap. Runs once, at conftest import time, before any
# pytest-asyncio loop is active.
asyncio.run(_bootstrap_test_db())


@pytest_asyncio.fixture(scope="session")
async def test_engine() -> AsyncIterator[AsyncEngine]:
    """Session-scoped shared engine. Uses the default connection pool so
    connections are reused across tests. Requires the session-scoped loop
    configured in pyproject.toml (``asyncio_default_test_loop_scope``)."""
    engine = create_async_engine(_TEST_DB_URL, echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Function-scoped session off the shared engine. Truncates the three
    tables at the end of each test so the next test starts with a clean slate.
    """
    session_factory = async_sessionmaker(
        test_engine, expire_on_commit=False, class_=AsyncSession
    )
    try:
        async with session_factory() as session:
            yield session
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE TABLE agent_actions, import_edges, code_index, repos "
                    "RESTART IDENTITY CASCADE"
                )
            )


# ---------------------------------------------------------------------------
# Shared fixture: index tests/fixtures/synthetic_py into the test DB.
# Available to any test via pytest's conftest inheritance. Used by the views
# tests and the agent tests.
# ---------------------------------------------------------------------------
from pathlib import Path as _Path  # noqa: E402

from gita.indexer.ingest import index_repository as _index_repository  # noqa: E402

_SYNTH_REPO_PATH = (
    _Path(__file__).parent / "fixtures" / "synthetic_py"
).resolve()
_SYNTH_REPO_NAME = "synthetic_py"


@pytest_asyncio.fixture
async def indexed_synth_py(
    db_session: AsyncSession,
) -> AsyncIterator[tuple[AsyncSession, str]]:
    """Ingest synthetic_py, commit, and yield ``(session, repo_name)``.

    Reused by tests in ``tests/views/`` and ``tests/agents/``. The outer
    ``db_session`` fixture truncates the three tables after each test.
    """
    await _index_repository(db_session, _SYNTH_REPO_NAME, _SYNTH_REPO_PATH)
    await db_session.commit()
    yield db_session, _SYNTH_REPO_NAME
