"""Tests for src.agents.pr_agent — context gathering, shared data, handle flow."""

import pytest
from unittest.mock import AsyncMock, patch

from src.agents.pr_agent import PRReviewAgent
from src.tools.base import ToolResult

from tests.conftest import make_agent_context


@pytest.fixture
def agent():
    with patch.object(PRReviewAgent, "__init__", lambda self, *a, **kw: None):
        a = PRReviewAgent.__new__(PRReviewAgent)
        a.name = "pr_reviewer"
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


class TestGatherContext:
    @patch("src.agents.pr_agent._get_focused_code_map", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._get_file_ownership", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._check_test_coverage", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._analyze_diff_quality", new_callable=AsyncMock)
    async def test_uses_shared_data(self, mock_quality, mock_coverage, mock_ownership, mock_codemap, agent):
        mock_ownership.return_value = ToolResult(success=True, data={"owner": "dev1"})
        mock_codemap.return_value = ToolResult(success=True, data="map text")
        mock_quality.return_value = ToolResult(success=True, data={"score": 8})
        mock_coverage.return_value = ToolResult(success=True, data={"covered": True})

        shared = {
            "files": [{"filename": "src/main.py"}],
            "diff": "+new line",
            "blast_radius": {"affected": 2},
        }
        gathered = await agent._gather_context(10, shared_data=shared)

        assert gathered["files"] == shared["files"]
        assert gathered["diff"] == shared["diff"]
        assert gathered["blast_radius"] == shared["blast_radius"]

    @patch("src.agents.pr_agent._get_focused_code_map", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._get_file_ownership", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._check_test_coverage", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._analyze_diff_quality", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._get_blast_radius", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._get_pr_diff", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._get_pr_files", new_callable=AsyncMock)
    async def test_fetches_independently_without_shared_data(
        self, mock_files, mock_diff, mock_blast, mock_quality, mock_coverage, mock_ownership, mock_codemap, agent
    ):
        mock_files.return_value = ToolResult(success=True, data=[{"filename": "a.py"}])
        mock_diff.return_value = ToolResult(success=True, data={"diff": "+line"})
        mock_blast.return_value = ToolResult(success=True, data={})
        mock_ownership.return_value = ToolResult(success=True, data={})
        mock_codemap.return_value = ToolResult(success=True, data="")
        mock_quality.return_value = ToolResult(success=True, data={})
        mock_coverage.return_value = ToolResult(success=True, data={})

        gathered = await agent._gather_context(10, shared_data=None)

        mock_files.assert_called_once()
        mock_diff.assert_called_once()
        assert gathered["files"] == [{"filename": "a.py"}]

    @patch("src.agents.pr_agent._get_focused_code_map", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._get_file_ownership", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._check_test_coverage", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._analyze_diff_quality", new_callable=AsyncMock)
    async def test_empty_diff_skips_ai_analysis(self, mock_quality, mock_coverage, mock_ownership, mock_codemap, agent):
        shared = {"files": [], "diff": "", "blast_radius": {}}
        mock_ownership.return_value = ToolResult(success=True, data={})
        mock_codemap.return_value = ToolResult(success=True, data="")

        gathered = await agent._gather_context(10, shared_data=shared)

        mock_quality.assert_not_called()
        mock_coverage.assert_not_called()
        assert gathered["quality_analysis"] == {}


class TestHandle:
    @patch("src.agents.pr_agent._get_focused_code_map", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._get_file_ownership", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._check_test_coverage", new_callable=AsyncMock)
    @patch("src.agents.pr_agent._analyze_diff_quality", new_callable=AsyncMock)
    async def test_handle_uses_shared_data_from_context(
        self, mock_quality, mock_coverage, mock_ownership, mock_codemap, agent
    ):
        mock_ownership.return_value = ToolResult(success=True, data={})
        mock_codemap.return_value = ToolResult(success=True, data="")
        mock_quality.return_value = ToolResult(success=True, data={})
        mock_coverage.return_value = ToolResult(success=True, data={})
        agent.run_tool_loop = AsyncMock(return_value=("Review done", []))

        ctx = make_agent_context(
            event_type="pull_request.opened",
            event_payload={"pull_request": {"number": 5, "title": "Fix bug", "user": {"login": "dev"}, "head": {"ref": "fix", "sha": "abc"}, "base": {"ref": "main"}}},
            additional_data={"pr_gathered": {"files": [{"filename": "f.py"}], "diff": "+line", "blast_radius": {}}},
        )
        result = await agent.handle(ctx)

        assert result.status == "success"
        assert result.data["pr_number"] == 5
        agent.run_tool_loop.assert_called_once()

    async def test_should_notify_when_comment_posted(self, agent):
        agent._gather_context = AsyncMock(return_value={
            "files": [], "diff": "", "blast_radius": {},
            "file_ownership": {}, "focused_code_map": "",
            "quality_analysis": {}, "test_coverage": {},
        })
        agent.run_tool_loop = AsyncMock(return_value=("Done", [
            {"tool": "post_comment", "result": {"success": True}},
        ]))

        ctx = make_agent_context(
            event_type="pull_request.opened",
            event_payload={"pull_request": {"number": 1, "title": "t", "user": {"login": "u"}, "head": {"ref": "h", "sha": "s"}, "base": {"ref": "main"}}},
        )
        result = await agent.handle(ctx)
        assert result.should_notify is True

    async def test_should_not_notify_when_no_output_tools(self, agent):
        agent._gather_context = AsyncMock(return_value={
            "files": [], "diff": "", "blast_radius": {},
            "file_ownership": {}, "focused_code_map": "",
            "quality_analysis": {}, "test_coverage": {},
        })
        agent.run_tool_loop = AsyncMock(return_value=("Done", [
            {"tool": "get_issue", "result": {"success": True}},
        ]))

        ctx = make_agent_context(
            event_type="pull_request.opened",
            event_payload={"pull_request": {"number": 1, "title": "t", "user": {"login": "u"}, "head": {"ref": "h", "sha": "s"}, "base": {"ref": "main"}}},
        )
        result = await agent.handle(ctx)
        assert result.should_notify is False
