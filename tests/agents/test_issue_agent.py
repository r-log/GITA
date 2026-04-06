"""Tests for src.agents.issue_agent — checklist validation, closure validation, handle routing."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.agents.issue_agent import IssueAnalystAgent, CHECKLIST_PATTERN
from src.tools.base import ToolResult

from tests.conftest import make_agent_context


@pytest.fixture
def agent():
    """Create an IssueAnalystAgent with mocked BaseAgent init."""
    with patch.object(IssueAnalystAgent, "__init__", lambda self, *a, **kw: None):
        a = IssueAnalystAgent.__new__(IssueAnalystAgent)
        a.name = "issue_analyst"
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


# ---------------------------------------------------------------------------
# CHECKLIST_PATTERN regex
# ---------------------------------------------------------------------------

class TestChecklistPattern:
    def test_matches_checked_item(self):
        match = CHECKLIST_PATTERN.findall("- [x] Implement auth (#5)")
        assert len(match) == 1
        assert match[0] == ("x", "Implement auth ", "5")

    def test_matches_unchecked_item(self):
        match = CHECKLIST_PATTERN.findall("- [ ] Setup CI (#12)")
        assert len(match) == 1
        assert match[0] == (" ", "Setup CI ", "12")

    def test_matches_capital_x(self):
        match = CHECKLIST_PATTERN.findall("- [X] Deploy (#3)")
        assert len(match) == 1
        assert match[0] == ("X", "Deploy ", "3")

    def test_matches_multiple_items(self):
        body = "- [x] Task A (#1)\n- [ ] Task B (#2)\n- [x] Task C (#3)"
        matches = CHECKLIST_PATTERN.findall(body)
        assert len(matches) == 3

    def test_no_match_without_issue_number(self):
        match = CHECKLIST_PATTERN.findall("- [x] Task without number")
        assert len(match) == 0


# ---------------------------------------------------------------------------
# _validate_checklist
# ---------------------------------------------------------------------------

class TestValidateChecklist:
    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._get_issue", new_callable=AsyncMock)
    async def test_no_checklist_items(self, mock_get, mock_update, mock_comment, agent):
        result = await agent._validate_checklist(1, {"body": "No checklist here"})
        assert result == {"fixed": False, "unchecked": []}
        mock_get.assert_not_called()

    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._get_issue", new_callable=AsyncMock)
    async def test_checked_item_still_open_gets_unchecked(self, mock_get, mock_update, mock_comment, agent):
        mock_get.return_value = ToolResult(success=True, data={"state": "open", "number": 5})
        mock_update.return_value = ToolResult(success=True, data={})

        issue_data = {"body": "- [x] Auth feature (#5)"}
        result = await agent._validate_checklist(1, issue_data)

        assert result["fixed"] is True
        assert 5 in result["unchecked"]
        mock_update.assert_called_once()
        mock_comment.assert_called_once()

    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._get_issue", new_callable=AsyncMock)
    async def test_checked_item_closed_stays_checked(self, mock_get, mock_update, mock_comment, agent):
        mock_get.return_value = ToolResult(success=True, data={"state": "closed", "number": 5})

        issue_data = {"body": "- [x] Auth feature (#5)"}
        result = await agent._validate_checklist(1, issue_data)

        assert result["fixed"] is False
        assert result["unchecked"] == []
        mock_update.assert_not_called()

    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._get_issue", new_callable=AsyncMock)
    async def test_get_issue_failure_skips_item(self, mock_get, mock_update, mock_comment, agent):
        mock_get.return_value = ToolResult(success=False, error="Not found")

        issue_data = {"body": "- [x] Auth feature (#5)"}
        result = await agent._validate_checklist(1, issue_data)

        assert result["fixed"] is False
        mock_update.assert_not_called()

    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._get_issue", new_callable=AsyncMock)
    async def test_update_failure_returns_not_fixed(self, mock_get, mock_update, mock_comment, agent):
        mock_get.return_value = ToolResult(success=True, data={"state": "open", "number": 5})
        mock_update.return_value = ToolResult(success=False, error="API error")

        issue_data = {"body": "- [x] Auth feature (#5)"}
        result = await agent._validate_checklist(1, issue_data)

        assert result["fixed"] is False

    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._get_issue", new_callable=AsyncMock)
    async def test_multiple_items_some_open(self, mock_get, mock_update, mock_comment, agent):
        async def get_issue_side_effect(inst, repo, num):
            if num == 5:
                return ToolResult(success=True, data={"state": "open", "number": 5})
            return ToolResult(success=True, data={"state": "closed", "number": num})

        mock_get.side_effect = get_issue_side_effect
        mock_update.return_value = ToolResult(success=True, data={})

        issue_data = {"body": "- [x] Task A (#3)\n- [x] Task B (#5)"}
        result = await agent._validate_checklist(1, issue_data)

        assert result["fixed"] is True
        assert 5 in result["unchecked"]
        assert 3 not in result["unchecked"]


# ---------------------------------------------------------------------------
# _validate_closure
# ---------------------------------------------------------------------------

class TestValidateClosure:
    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent.GitHubClient")
    async def test_milestone_tracker_skipped(self, mock_client_cls, mock_update, mock_comment, agent):
        issue_data = {"labels": [{"name": "Milestone Tracker"}]}
        result = await agent._validate_closure(1, issue_data)

        assert result["reopened"] is False
        assert result["reason"] == "milestone_tracker"
        mock_client_cls.assert_not_called()

    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent.GitHubClient")
    async def test_with_linked_pr_stays_closed(self, mock_client_cls, mock_update, mock_comment, agent):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=[
            {"event": "cross-referenced", "source": {"issue": {"pull_request": {"url": "..."}}}}
        ])
        mock_client_cls.return_value = mock_client

        issue_data = {"labels": []}
        result = await agent._validate_closure(1, issue_data)

        assert result["reopened"] is False
        assert result["reason"] == "has_linked_pr"

    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent.GitHubClient")
    async def test_no_linked_pr_reopens(self, mock_client_cls, mock_update, mock_comment, agent):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=[])  # No events
        mock_client_cls.return_value = mock_client
        mock_update.return_value = ToolResult(success=True, data={})

        issue_data = {"labels": []}
        result = await agent._validate_closure(1, issue_data)

        assert result["reopened"] is True
        assert result["reason"] == "no_linked_pr"
        mock_update.assert_called_once()
        mock_comment.assert_called_once()

    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent.GitHubClient")
    async def test_timeline_failure_does_not_reopen(self, mock_client_cls, mock_update, mock_comment, agent):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("API error"))
        mock_client_cls.return_value = mock_client

        issue_data = {"labels": []}
        result = await agent._validate_closure(1, issue_data)

        assert result["reopened"] is False
        assert result["reason"] == "timeline_check_failed"

    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent.GitHubClient")
    async def test_reopen_failure(self, mock_client_cls, mock_update, mock_comment, agent):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=[])
        mock_client_cls.return_value = mock_client
        mock_update.return_value = ToolResult(success=False, error="API error")

        issue_data = {"labels": []}
        result = await agent._validate_closure(1, issue_data)

        assert result["reopened"] is False
        assert result["reason"] == "reopen_failed"


# ---------------------------------------------------------------------------
# handle() — routing logic
# ---------------------------------------------------------------------------

class TestHandle:
    @patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock)
    @patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock)
    @patch("src.agents.issue_agent.GitHubClient")
    async def test_closed_event_triggers_closure_validation(self, mock_client_cls, mock_update, mock_comment, agent):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=[
            {"event": "connected"}
        ])
        mock_client_cls.return_value = mock_client

        ctx = make_agent_context(
            event_type="issues.closed",
            event_payload={"issue": {"number": 5, "labels": [], "body": ""}},
        )
        result = await agent.handle(ctx)

        assert result.status == "success"
        assert result.data["closure_result"]["reason"] == "has_linked_pr"

    async def test_tracker_edit_triggers_checklist_validation(self, agent):
        with patch("src.agents.issue_agent._get_issue", new_callable=AsyncMock) as mock_get, \
             patch("src.agents.issue_agent._update_issue", new_callable=AsyncMock) as mock_update, \
             patch("src.agents.issue_agent._post_comment", new_callable=AsyncMock):
            mock_get.return_value = ToolResult(success=True, data={"state": "open", "number": 3})
            mock_update.return_value = ToolResult(success=True, data={})

            ctx = make_agent_context(
                event_type="issues.edited",
                event_payload={
                    "issue": {
                        "number": 1,
                        "labels": [{"name": "Milestone Tracker"}],
                        "body": "- [x] Task (#3)",
                    }
                },
            )
            result = await agent.handle(ctx)

            assert result.status == "success"
            assert result.confidence == 1.0

    async def test_non_deterministic_event_uses_llm(self, agent):
        agent.run_tool_loop = AsyncMock(return_value=("Analysis complete", [
            {"tool": "evaluate_smart", "result": {"success": True}},
        ]))

        ctx = make_agent_context(
            event_type="issues.opened",
            event_payload={"issue": {"number": 10, "labels": [], "body": "Add login"}},
        )
        result = await agent.handle(ctx)

        assert result.status == "success"
        agent.run_tool_loop.assert_called_once()
