"""Tests for src.agents.progress_agent — tracker detection, gathering, handle flow."""

import pytest
from unittest.mock import AsyncMock, patch

from src.agents.progress_agent import ProgressTrackerAgent, _CHECKLIST_RE
from src.tools.base import ToolResult

from tests.conftest import make_agent_context


@pytest.fixture
def agent():
    with patch.object(ProgressTrackerAgent, "__init__", lambda self, *a, **kw: None):
        a = ProgressTrackerAgent.__new__(ProgressTrackerAgent)
        a.name = "progress_tracker"
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


class TestChecklistRegex:
    def test_matches_checked(self):
        matches = _CHECKLIST_RE.findall("- [x] Task (#5)")
        assert len(matches) == 1
        assert matches[0] == ("x", "5")

    def test_matches_unchecked(self):
        matches = _CHECKLIST_RE.findall("- [ ] Task (#5)")
        assert len(matches) == 1
        assert matches[0] == (" ", "5")

    def test_multiple_items(self):
        body = "- [x] Done (#1)\n- [ ] Todo (#2)\n- [X] Also done (#3)"
        matches = _CHECKLIST_RE.findall(body)
        assert len(matches) == 3


class TestGatherContext:
    @patch("src.agents.progress_agent._detect_stale_prs", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._get_open_prs", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._get_milestone_file_coverage", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._detect_blockers", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._calculate_velocity", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._get_all_issues", new_callable=AsyncMock)
    async def test_finds_milestone_trackers(
        self, mock_issues, mock_velocity, mock_blockers, mock_coverage, mock_prs, mock_stale, agent
    ):
        mock_issues.return_value = ToolResult(success=True, data=[
            {
                "number": 1, "title": "Milestone 1", "state": "open",
                "labels": [{"name": "Milestone Tracker"}],
                "body": "- [x] Task A (#2)\n- [ ] Task B (#3)",
            },
            {"number": 2, "title": "Task A", "state": "closed", "labels": []},
            {"number": 3, "title": "Task B", "state": "open", "labels": []},
        ])
        mock_velocity.return_value = ToolResult(success=True, data={"rate": 2.0})
        mock_blockers.return_value = ToolResult(success=True, data={"blockers": []})
        mock_coverage.return_value = ToolResult(success=True, data={})
        mock_prs.return_value = ToolResult(success=True, data=[])
        mock_stale.return_value = ToolResult(success=True, data={})

        gathered = await agent._gather_context()

        assert len(gathered["trackers"]) == 1
        tracker = gathered["trackers"][0]
        assert tracker["number"] == 1
        assert tracker["total_tasks"] == 2
        assert tracker["completed_tasks"] == 1

    @patch("src.agents.progress_agent._detect_stale_prs", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._get_open_prs", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._get_milestone_file_coverage", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._detect_blockers", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._calculate_velocity", new_callable=AsyncMock)
    @patch("src.agents.progress_agent._get_all_issues", new_callable=AsyncMock)
    async def test_focus_filters_trackers(
        self, mock_issues, mock_velocity, mock_blockers, mock_coverage, mock_prs, mock_stale, agent
    ):
        mock_issues.return_value = ToolResult(success=True, data=[
            {"number": 1, "title": "M1", "state": "open", "labels": [{"name": "Milestone Tracker"}], "body": ""},
            {"number": 2, "title": "M2", "state": "open", "labels": [{"name": "Milestone Tracker"}], "body": ""},
        ])
        mock_velocity.return_value = ToolResult(success=True, data={})
        mock_blockers.return_value = ToolResult(success=True, data={})
        mock_coverage.return_value = ToolResult(success=True, data={})
        mock_prs.return_value = ToolResult(success=True, data=[])
        mock_stale.return_value = ToolResult(success=True, data={})

        gathered = await agent._gather_context(focus_milestone_number=1)

        assert len(gathered["trackers"]) == 1
        assert gathered["trackers"][0]["number"] == 1


class TestHandle:
    async def test_extracts_focus_from_milestone_payload(self, agent):
        agent._gather_context = AsyncMock(return_value={"trackers": [], "open_prs": {}})
        agent.run_tool_loop = AsyncMock(return_value=("Report", []))

        ctx = make_agent_context(
            event_type="issues.milestoned",
            event_payload={
                "issue": {"number": 5, "milestone": {"number": 3, "title": "v1.0"}},
            },
        )
        result = await agent.handle(ctx)

        assert result.status == "success"
        # _gather_context should have been called with focus=3
        agent._gather_context.assert_called_once_with(3)

    async def test_no_focus_when_no_milestone(self, agent):
        agent._gather_context = AsyncMock(return_value={"trackers": [], "open_prs": {}})
        agent.run_tool_loop = AsyncMock(return_value=("Report", []))

        ctx = make_agent_context(
            event_type="push",
            event_payload={},
        )
        await agent.handle(ctx)

        agent._gather_context.assert_called_once_with(None)
