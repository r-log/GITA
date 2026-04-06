"""Tests for src.workers.tasks — event dispatch."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.workers.tasks import dispatch_event, _get_supervisor
from src.agents.base import AgentResult


class TestGetSupervisor:
    def test_returns_supervisor(self):
        sup = _get_supervisor()
        assert sup is not None
        assert sup.name == "supervisor"

    def test_returns_singleton(self):
        sup1 = _get_supervisor()
        sup2 = _get_supervisor()
        assert sup1 is sup2


class TestDispatchEvent:
    @patch("src.workers.tasks._get_supervisor")
    @patch("src.workers.tasks.upsert_repository", new_callable=AsyncMock)
    async def test_normal_repo_event(self, mock_upsert, mock_get_sup):
        mock_upsert.return_value = 42

        mock_sup = AsyncMock()
        mock_sup.handle = AsyncMock(return_value=AgentResult(
            agent_name="supervisor", status="success", data={"agent_results": {}},
        ))
        mock_get_sup.return_value = mock_sup

        payload = {"repository": {"id": 999, "full_name": "owner/repo"}}
        await dispatch_event("issues", "opened", "owner/repo", 1001, payload)

        mock_upsert.assert_called_once_with(999, "owner/repo", 1001)
        mock_sup.handle.assert_called_once()

    @patch("src.workers.tasks._get_supervisor")
    @patch("src.workers.tasks.upsert_repository", new_callable=AsyncMock)
    async def test_installation_event_dispatches_per_repo(self, mock_upsert, mock_get_sup):
        mock_upsert.return_value = 1

        mock_sup = AsyncMock()
        mock_sup.handle = AsyncMock(return_value=AgentResult(
            agent_name="supervisor", status="success", data={"agent_results": {}},
        ))
        mock_get_sup.return_value = mock_sup

        payload = {
            "repositories": [
                {"id": 100, "full_name": "owner/repo-a"},
                {"id": 200, "full_name": "owner/repo-b"},
            ],
        }
        await dispatch_event("installation", "created", "", 1001, payload)

        assert mock_upsert.call_count == 2
        assert mock_sup.handle.call_count == 2

    @patch("src.workers.tasks._get_supervisor")
    @patch("src.workers.tasks.upsert_repository", new_callable=AsyncMock)
    async def test_event_type_includes_action(self, mock_upsert, mock_get_sup):
        mock_upsert.return_value = 42
        mock_sup = AsyncMock()
        mock_sup.handle = AsyncMock(return_value=AgentResult(
            agent_name="supervisor", status="success", data={"agent_results": {}},
        ))
        mock_get_sup.return_value = mock_sup

        payload = {"repository": {"id": 999}}
        await dispatch_event("issues", "closed", "owner/repo", 1001, payload)

        # The context should have event_type = "issues.closed"
        ctx = mock_sup.handle.call_args[0][0]
        assert ctx.event_type == "issues.closed"
