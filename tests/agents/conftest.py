"""Agent-specific test fixtures."""

from unittest.mock import AsyncMock, patch

import pytest

from src.agents.base import AgentResult


@pytest.fixture
def mock_run_tool_loop():
    """
    Fixture that patches run_tool_loop on an agent instance.

    Usage:
        agent = SomeAgent(...)
        mock_run_tool_loop(agent, final_text="Analysis complete", tool_log=[...])
    """
    def _patch(agent, final_text="Agent done.", tool_log=None):
        agent.run_tool_loop = AsyncMock(return_value=(final_text, tool_log or []))
    return _patch


@pytest.fixture
def mock_registry():
    """Patches the global registry to return controlled agents."""
    with patch("src.agents.supervisor.registry") as mock_reg:
        yield mock_reg


@pytest.fixture
def mock_async_session():
    """Patches async_session specifically for supervisor DB calls."""
    with patch("src.agents.supervisor.async_session") as mock_sess:
        session = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_sess.return_value = ctx
        yield session
