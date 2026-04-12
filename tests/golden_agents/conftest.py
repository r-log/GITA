"""Gate the golden-agent tests behind ``GITA_RUN_LLM_TESTS=1``.

These tests call the real onboarding agent, which makes **real OpenRouter
calls that cost money**. They should only run when the developer
explicitly flips a flag, not on every ``pytest`` run.

The ``test_checklist.py`` module in the same directory is a pure unit test
of the checklist runner — no LLM — and is NOT gated.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from gita.config import settings
from gita.llm.client import OpenRouterClient

RUN_LLM_TESTS = os.environ.get("GITA_RUN_LLM_TESTS") == "1"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip every ``test_onboarding_*`` under ``tests/golden_agents/``
    unless ``GITA_RUN_LLM_TESTS=1`` is set in the environment."""
    if RUN_LLM_TESTS:
        return

    skip_marker = pytest.mark.skip(
        reason=(
            "set GITA_RUN_LLM_TESTS=1 to run tests that call the real LLM"
        )
    )
    for item in items:
        # Only gate onboarding agent tests; the checklist unit tests
        # run unconditionally.
        if "golden_agents/test_onboarding_" in item.nodeid.replace("\\", "/"):
            item.add_marker(skip_marker)


@pytest_asyncio.fixture
async def real_llm() -> AsyncIterator[OpenRouterClient]:
    """Real OpenRouter client for golden-agent tests. Only instantiated when
    ``GITA_RUN_LLM_TESTS=1`` (otherwise the tests themselves are skipped)."""
    if not settings.openrouter_api_key:
        pytest.skip("OPENROUTER_API_KEY not configured")
    async with OpenRouterClient(
        api_key=settings.openrouter_api_key,
        default_model=settings.ai_default_model,
    ) as client:
        yield client
