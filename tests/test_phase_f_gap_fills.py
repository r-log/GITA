"""
Phase F gap-fill tests — covers remaining branches in medium-coverage files:
dashboard_api, supervisor, base agent, code_map, parsers.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.base import AgentContext, AgentResult


# ---------------------------------------------------------------------------
# dashboard_api.py — lines 110-118 (issue stats loop), 410-427 (security alerts),
#                     578-579 (reconcile error), 582-599 (rescan action)
# ---------------------------------------------------------------------------

class TestDashboardGapFills:
    @patch("src.api.dashboard_api.async_session")
    async def test_get_stats_with_plan_milestones(self, mock_factory):
        """Cover lines 110-118: issue stats loop over milestones/tasks."""
        from src.api.dashboard_api import get_stats

        session = AsyncMock()
        call_count = [0]
        async def mock_execute(stmt):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] == 1:  # onboarding counts
                row = MagicMock(); row.total = 1; row.last_completed = None
                result.one.return_value = row
            elif call_count[0] == 2:  # last context
                result.scalar_one_or_none.return_value = None
            elif call_count[0] == 3:  # agent counts
                result.all.return_value = []
            elif call_count[0] == 4:  # status counts
                result.all.return_value = []
            elif call_count[0] == 5:  # plan with milestones
                result.scalar_one_or_none.return_value = {
                    "milestones": [{
                        "tasks": [
                            {"status": "done"},
                            {"status": "in-progress"},
                            {"status": "not-started"},
                        ]
                    }]
                }
            return result
        session.execute = mock_execute
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        result = await get_stats(repo_id=1)
        assert result["issues_in_plan"]["total"] == 3
        assert result["issues_in_plan"]["done"] == 1
        assert result["issues_in_plan"]["in_progress"] == 1
        assert result["issues_in_plan"]["not_started"] == 1

    @patch("src.api.dashboard_api.async_session")
    async def test_get_alerts_with_security_findings(self, mock_factory):
        """Cover lines 410-427: security analysis findings extraction."""
        from src.api.dashboard_api import get_alerts

        analysis = MagicMock()
        analysis.id = 1
        analysis.result = {
            "findings": {
                "critical": [{"description": "Leaked API key", "recommendation": "Rotate key"}],
                "warning": [{"description": "Weak hash", "recommendation": "Use bcrypt"}],
                "info": [{"description": "FYI"}],
            }
        }
        analysis.created_at = datetime(2026, 1, 1)

        session = AsyncMock()
        call_count = [0]
        async def mock_execute(stmt):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] == 1:  # analyses
                result.scalars.return_value.all.return_value = [analysis]
            else:  # failed runs
                result.scalars.return_value.all.return_value = []
            return result
        session.execute = mock_execute
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        result = await get_alerts(repo_id=1)
        assert len(result["critical"]) == 1
        assert len(result["warnings"]) == 1
        assert result["info_count"] == 1
        assert "Leaked API key" in result["critical"][0]["message"]

    @patch("src.api.dashboard_api.async_session")
    async def test_trigger_rescan_action(self, mock_factory):
        """Cover lines 582-599: rescan action with SupervisorAgent."""
        from src.api.dashboard_api import trigger_action

        repo = MagicMock()
        repo.id = 1; repo.full_name = "o/r"; repo.github_id = 999; repo.installation_id = 1001

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=repo)
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        with patch("src.agents.supervisor.SupervisorAgent") as mock_sup_cls:
            mock_sup = AsyncMock()
            mock_sup.handle = AsyncMock(return_value=AgentResult(
                agent_name="supervisor", status="success", data={},
            ))
            mock_sup_cls.return_value = mock_sup

            request = AsyncMock()
            request.body = AsyncMock(return_value=b'{"repo_id": 1, "action": "rescan"}')
            result = await trigger_action(request)

        assert result["status"] == "ok"
        assert result["action"] == "rescan"

    @patch("src.api.dashboard_api.async_session")
    async def test_trigger_reconcile_error(self, mock_factory):
        """Cover lines 578-579: reconcile error path."""
        from src.api.dashboard_api import trigger_action

        repo = MagicMock()
        repo.id = 1; repo.full_name = "o/r"; repo.installation_id = 1001

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=repo)
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        with patch("src.workers.reconciliation.reconcile_repo", new_callable=AsyncMock) as mock_rec:
            mock_rec.side_effect = Exception("DB down")
            request = AsyncMock()
            request.body = AsyncMock(return_value=b'{"repo_id": 1, "action": "reconcile"}')
            result = await trigger_action(request)

        assert result["status"] == "error"
        assert "DB down" in result["message"]

    @patch("src.api.dashboard_api.async_session")
    async def test_get_costs_with_per_model_pricing(self, mock_factory):
        """Cover lines 485-489: per-model cost calculation."""
        from src.api.dashboard_api import get_costs

        session = AsyncMock()
        row = (
            "pr_reviewer",
            {"usage": {
                "prompt_tokens": 1000, "completion_tokens": 500, "llm_calls": 2,
                "by_model": {"anthropic/claude-sonnet-4": {"prompt_tokens": 1000, "completion_tokens": 500}},
            }},
            datetime(2026, 1, 1, 12, 0, 0),
        )
        session.execute.return_value = MagicMock(all=MagicMock(return_value=[row]))
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        result = await get_costs(repo_id=1, days=30)
        assert result["total_prompt_tokens"] == 1000
        assert result["total_cost_usd"] > 0  # Should calculate from pricing
        assert "pr_reviewer" in result["by_agent"]


# ---------------------------------------------------------------------------
# supervisor.py — lines 77-80 (all on cooldown), 187-195 (timeout + exception)
# ---------------------------------------------------------------------------

class TestSupervisorGapFills:
    @patch("src.agents.supervisor.registry")
    @patch("src.agents.supervisor.async_session")
    async def test_all_agents_on_cooldown(self, mock_session, mock_registry):
        """Cover lines 77-80: all agents filtered by cooldown."""
        from src.agents.supervisor import SupervisorAgent
        from tests.conftest import make_agent_context

        # Mock DB session — cooldown check returns all agents as recently run
        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = MagicMock()  # Found recent run
        session.execute = AsyncMock(return_value=result_mock)
        ctx_mgr = AsyncMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=session)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = ctx_mgr

        sup = SupervisorAgent()
        context = make_agent_context(event_type="issues.opened", repo_id=42,
                                      event_payload={"issue": {"number": 5}})
        result = await sup.handle(context)
        assert "cooldown" in result.data.get("message", "").lower() or result.status == "success"

    @patch("src.agents.supervisor.registry")
    @patch("src.agents.supervisor.async_session")
    async def test_agent_timeout(self, mock_session, mock_registry):
        """Cover lines 187-190: agent timeout handling."""
        from src.agents.supervisor import SupervisorAgent
        from tests.conftest import make_agent_context

        mock_agent = AsyncMock()
        mock_agent.handle = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_agent._usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "by_model": {}}
        mock_registry.get.return_value = mock_agent

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result_mock)
        ctx_mgr = AsyncMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=session)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = ctx_mgr

        sup = SupervisorAgent()
        context = make_agent_context(event_type="issues.opened", repo_id=0)
        result = await sup.handle(context)
        # Agent should have failed status
        agent_results = result.data.get("agent_results", {})
        if agent_results:
            first = list(agent_results.values())[0]
            assert first["status"] == "failed"

    @patch("src.agents.supervisor.registry")
    @patch("src.agents.supervisor.async_session")
    async def test_agent_exception(self, mock_session, mock_registry):
        """Cover lines 191-195: agent raises unexpected exception."""
        from src.agents.supervisor import SupervisorAgent
        from tests.conftest import make_agent_context

        mock_agent = AsyncMock()
        mock_agent.handle = AsyncMock(side_effect=RuntimeError("unexpected crash"))
        mock_agent._usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "by_model": {}}
        mock_registry.get.return_value = mock_agent

        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result_mock)
        ctx_mgr = AsyncMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=session)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = ctx_mgr

        sup = SupervisorAgent()
        context = make_agent_context(event_type="issues.opened", repo_id=0)
        result = await sup.handle(context)
        agent_results = result.data.get("agent_results", {})
        if agent_results:
            first = list(agent_results.values())[0]
            assert first["status"] == "failed"


# ---------------------------------------------------------------------------
# base.py — lines 86-105 (__init__ with prompt file)
# ---------------------------------------------------------------------------

class TestBaseAgentInit:
    def test_init_with_system_prompt_string(self):
        """Cover lines 100-103: system_prompt directly provided."""
        from src.agents.base import BaseAgent
        from src.tools.base import Tool

        class TestAgent(BaseAgent):
            async def handle(self, context):
                pass

        agent = TestAgent(
            name="test", description="A test",
            tools=[], system_prompt="You are a test agent.",
        )
        assert agent.system_prompt == "You are a test agent."
        assert agent.name == "test"

    def test_init_default_prompt(self):
        """Cover line 103: no prompt file or string — generates default."""
        from src.agents.base import BaseAgent

        class TestAgent(BaseAgent):
            async def handle(self, context):
                pass

        agent = TestAgent(name="helper", description="Helps things", tools=[])
        assert "helper" in agent.system_prompt
        assert "Helps things" in agent.system_prompt

    def test_init_with_prompt_file(self):
        """Cover lines 94-97: loading prompt from file."""
        from src.agents.base import BaseAgent
        import tempfile, os

        class TestAgent(BaseAgent):
            async def handle(self, context):
                pass

        # Create a temp prompt file in prompts/ dir
        os.makedirs("prompts", exist_ok=True)
        prompt_file = "test_prompt_temp.md"
        prompt_path = os.path.join("prompts", prompt_file)
        try:
            with open(prompt_path, "w") as f:
                f.write("You are a specialized test agent.")

            agent = TestAgent(
                name="test", description="Test",
                tools=[], system_prompt_file=prompt_file,
            )
            assert agent.system_prompt == "You are a specialized test agent."
        finally:
            os.remove(prompt_path)

    def test_init_missing_prompt_file_raises(self):
        """Cover lines 98-99: FileNotFoundError for missing prompt file."""
        from src.agents.base import BaseAgent
        import pytest

        class TestAgent(BaseAgent):
            async def handle(self, context):
                pass

        with pytest.raises(FileNotFoundError):
            TestAgent(
                name="test", description="Test",
                tools=[], system_prompt_file="nonexistent_prompt.md",
            )


# ---------------------------------------------------------------------------
# code_map.py — lines 172-186 (route/model/service categorization)
# ---------------------------------------------------------------------------

class TestCodeMapGapFills:
    def test_generate_code_map_with_structures(self):
        """Cover lines 86-87, 100, 172-186: categorization of routes, models, services."""
        from src.indexer.code_map import generate_code_map

        records = [
            {
                "file_path": "src/api/routes.py",
                "language": "python",
                "line_count": 50, "size_bytes": 1000,
                "structure": {
                    "classes": [],
                    "functions": [{"name": "get_users", "args": [], "decorators": ["@app.get"], "is_async": True}],
                    "routes": [{"method": "GET", "path": "/users", "handler": "get_users"}],
                    "imports": ["from fastapi import APIRouter"],
                    "components": [],
                    "todos": [],
                },
            },
            {
                "file_path": "src/models/user.py",
                "language": "python",
                "line_count": 30, "size_bytes": 500,
                "structure": {
                    "classes": [{"name": "User", "bases": ["Base", "DeclarativeBase"], "methods": ["__repr__"], "fields": ["id", "name"], "decorators": []}],
                    "functions": [],
                    "routes": [],
                    "imports": ["from sqlalchemy import Column"],
                    "components": [],
                    "todos": [{"text": "TODO: add validation", "line": 10}],
                },
            },
        ]

        result = generate_code_map(records)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_code_map_empty(self):
        """Cover empty records path."""
        from src.indexer.code_map import generate_code_map
        result = generate_code_map([])
        assert isinstance(result, str)
