"""Tests for src.agents.risk_agent — security scanning, shared data, handle flow."""

import pytest
from unittest.mock import AsyncMock, patch

from src.agents.risk_agent import RiskDetectiveAgent, _DEP_FILES
from src.tools.base import ToolResult

from tests.conftest import make_agent_context


@pytest.fixture
def agent():
    with patch.object(RiskDetectiveAgent, "__init__", lambda self, *a, **kw: None):
        a = RiskDetectiveAgent.__new__(RiskDetectiveAgent)
        a.name = "risk_detective"
        a.description = "test"
        a.tools = []
        a.model = "test-model"
        a._usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "by_model": {}}
        a._tool_map = {}
        a.system_prompt = "test prompt"
        a._client = AsyncMock()
        a.installation_id = 1001
        a.repo_full_name = "owner/repo"
        a.repo_id = 42
        return a


class TestDepFiles:
    def test_common_dep_files_present(self):
        assert "package.json" in _DEP_FILES
        assert "requirements.txt" in _DEP_FILES
        assert "pyproject.toml" in _DEP_FILES
        assert "go.mod" in _DEP_FILES
        assert "Cargo.toml" in _DEP_FILES


class TestGatherContext:
    @patch("src.agents.risk_agent._get_open_prs", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._scan_secrets", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._scan_security_patterns", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._detect_breaking_changes", new_callable=AsyncMock)
    async def test_uses_shared_data(self, mock_breaking, mock_patterns, mock_secrets, mock_prs, agent):
        mock_secrets.return_value = ToolResult(success=True, data={"findings": []})
        mock_patterns.return_value = ToolResult(success=True, data={"patterns": []})
        mock_breaking.return_value = ToolResult(success=True, data={})
        mock_prs.return_value = ToolResult(success=True, data=[])

        shared = {
            "files": [{"filename": "src/auth.py"}],
            "diff": "+secret_key = 'abc'",
            "blast_radius": {"affected": 1},
        }
        gathered = await agent._gather_context(10, shared_data=shared)

        assert gathered["files"] == shared["files"]
        assert gathered["diff"] == shared["diff"]
        mock_secrets.assert_called_once()

    @patch("src.agents.risk_agent._get_open_prs", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._scan_secrets", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._scan_security_patterns", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._detect_breaking_changes", new_callable=AsyncMock)
    async def test_empty_diff_skips_scans(self, mock_breaking, mock_patterns, mock_secrets, mock_prs, agent):
        mock_prs.return_value = ToolResult(success=True, data=[])

        shared = {"files": [], "diff": "", "blast_radius": {}}
        gathered = await agent._gather_context(10, shared_data=shared)

        mock_secrets.assert_not_called()
        assert gathered["secrets_scan"] == {}

    @patch("src.agents.risk_agent._get_open_prs", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._check_dependency_changes", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._scan_secrets", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._scan_security_patterns", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._detect_breaking_changes", new_callable=AsyncMock)
    async def test_dep_files_trigger_dependency_check(
        self, mock_breaking, mock_patterns, mock_secrets, mock_dep, mock_prs, agent
    ):
        mock_secrets.return_value = ToolResult(success=True, data={})
        mock_patterns.return_value = ToolResult(success=True, data={})
        mock_breaking.return_value = ToolResult(success=True, data={})
        mock_dep.return_value = ToolResult(success=True, data={"added": ["new-pkg"]})
        mock_prs.return_value = ToolResult(success=True, data=[])

        shared = {
            "files": [{"filename": "package.json"}, {"filename": "src/index.js"}],
            "diff": "+new dep",
            "blast_radius": {},
        }
        gathered = await agent._gather_context(10, shared_data=shared)

        mock_dep.assert_called_once()
        assert gathered["dependency_changes"] == {"added": ["new-pkg"]}

    @patch("src.agents.risk_agent._get_open_prs", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._scan_secrets", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._scan_security_patterns", new_callable=AsyncMock)
    @patch("src.agents.risk_agent._detect_breaking_changes", new_callable=AsyncMock)
    async def test_no_dep_files_skips_dependency_check(self, mock_breaking, mock_patterns, mock_secrets, mock_prs, agent):
        mock_secrets.return_value = ToolResult(success=True, data={})
        mock_patterns.return_value = ToolResult(success=True, data={})
        mock_breaking.return_value = ToolResult(success=True, data={})
        mock_prs.return_value = ToolResult(success=True, data=[])

        shared = {
            "files": [{"filename": "src/app.py"}],
            "diff": "+code change",
            "blast_radius": {},
        }
        gathered = await agent._gather_context(10, shared_data=shared)
        assert gathered["dependency_changes"] == {}


class TestHandle:
    async def test_handle_returns_success(self, agent):
        agent._gather_context = AsyncMock(return_value={
            "files": [], "diff": "", "blast_radius": {},
            "secrets_scan": {}, "security_patterns": {},
            "breaking_changes": {}, "dependency_changes": {},
        })
        agent.run_tool_loop = AsyncMock(return_value=("No risks found", []))

        ctx = make_agent_context(
            event_type="pull_request.opened",
            event_payload={"pull_request": {"number": 5, "title": "Fix", "user": {"login": "dev"}, "head": {"sha": "abc"}}},
        )
        result = await agent.handle(ctx)

        assert result.status == "success"
        assert result.confidence == 0.85

    async def test_handle_notifies_on_check_run(self, agent):
        agent._gather_context = AsyncMock(return_value={})
        agent.run_tool_loop = AsyncMock(return_value=("Critical!", [
            {"tool": "create_check_run", "result": {"success": True}},
        ]))

        ctx = make_agent_context(
            event_type="pull_request.opened",
            event_payload={"pull_request": {"number": 5, "title": "Fix", "user": {"login": "dev"}, "head": {"sha": "abc"}}},
        )
        result = await agent.handle(ctx)
        assert result.should_notify is True
