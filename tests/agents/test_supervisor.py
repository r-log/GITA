"""Tests for src.agents.supervisor — routing, dispatch, merging, cooldown."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.base import AgentContext, AgentResult
from src.agents.supervisor import SupervisorAgent, ROUTING_TABLE
from src.tools.base import ToolResult

from tests.conftest import make_agent_context, make_agent_result


@pytest.fixture
def supervisor():
    return SupervisorAgent()


# ---------------------------------------------------------------------------
# _classify_and_plan — static routing table lookup
# ---------------------------------------------------------------------------

class TestClassifyAndPlan:
    @pytest.mark.parametrize("event_type,expected_agents,expected_parallel", [
        ("installation.created", ["onboarding"], False),
        ("installation_repositories.added", ["onboarding"], False),
        ("pull_request.opened", ["pr_reviewer", "risk_detective"], True),
        ("pull_request.synchronize", ["pr_reviewer", "risk_detective"], True),
        ("issues.opened", ["issue_analyst"], False),
        ("issues.edited", ["issue_analyst", "progress_tracker"], True),
        ("issues.assigned", ["issue_analyst"], False),
        ("issues.closed", ["issue_analyst", "progress_tracker"], True),
        ("issues.milestoned", ["issue_analyst", "progress_tracker"], True),
        ("push", ["progress_tracker", "risk_detective"], True),
        ("issue_comment.created", ["issue_analyst"], False),
    ])
    def test_routing_table_entries(self, supervisor, event_type, expected_agents, expected_parallel):
        ctx = make_agent_context(event_type=event_type)
        plan = supervisor._classify_and_plan(ctx)

        assert plan["agents_to_dispatch"] == expected_agents
        assert plan["parallel"] == expected_parallel

    def test_unknown_event_returns_empty(self, supervisor):
        ctx = make_agent_context(event_type="unknown_event.action")
        plan = supervisor._classify_and_plan(ctx)

        assert plan["agents_to_dispatch"] == []
        assert plan["parallel"] is False


# ---------------------------------------------------------------------------
# _extract_target_number
# ---------------------------------------------------------------------------

class TestExtractTargetNumber:
    def test_issue_payload(self, supervisor):
        payload = {"issue": {"number": 42}}
        assert supervisor._extract_target_number(payload) == 42

    def test_pull_request_payload(self, supervisor):
        payload = {"pull_request": {"number": 10}}
        assert supervisor._extract_target_number(payload) == 10

    def test_milestone_payload(self, supervisor):
        payload = {"milestone": {"number": 3}}
        assert supervisor._extract_target_number(payload) == 3

    def test_no_number_returns_none(self, supervisor):
        payload = {"action": "opened"}
        assert supervisor._extract_target_number(payload) is None

    def test_issue_takes_precedence(self, supervisor):
        """When both issue and PR are present, issue is checked first."""
        payload = {"issue": {"number": 5}, "pull_request": {"number": 10}}
        assert supervisor._extract_target_number(payload) == 5


# ---------------------------------------------------------------------------
# _merge_results
# ---------------------------------------------------------------------------

class TestMergeResults:
    def test_single_result_passthrough(self, supervisor):
        result = make_agent_result(
            agent_name="issue_analyst",
            status="success",
            actions_taken=[{"action": "commented"}],
            recommendations=["Consider labels"],
        )
        plan = {"agents_to_dispatch": ["issue_analyst"]}

        merged = supervisor._merge_results([result], plan)
        assert merged.status == "success"
        assert len(merged.actions_taken) == 1
        assert len(merged.recommendations) == 1

    def test_multiple_results_merged(self, supervisor):
        r1 = make_agent_result(
            agent_name="pr_reviewer", status="success",
            actions_taken=[{"action": "reviewed"}],
        )
        r2 = make_agent_result(
            agent_name="risk_detective", status="needs_review",
            actions_taken=[{"action": "scanned"}],
            recommendations=["Fix vulnerability"],
        )
        plan = {"agents_to_dispatch": ["pr_reviewer", "risk_detective"]}

        merged = supervisor._merge_results([r1, r2], plan)
        assert len(merged.actions_taken) == 2
        assert len(merged.recommendations) == 1

    def test_worst_status_wins(self, supervisor):
        """failed > needs_review > partial > success"""
        results = [
            make_agent_result(status="success"),
            make_agent_result(status="partial"),
            make_agent_result(status="failed"),
        ]
        merged = supervisor._merge_results(results, {})
        assert merged.status == "failed"

    def test_comment_bodies_joined(self, supervisor):
        r1 = make_agent_result(should_notify=True, comment_body="Part 1")
        r2 = make_agent_result(should_notify=True, comment_body="Part 2")

        merged = supervisor._merge_results([r1, r2], {})
        assert merged.should_notify is True
        assert "Part 1" in merged.comment_body
        assert "Part 2" in merged.comment_body
        assert "---" in merged.comment_body  # separator

    def test_no_comments_yields_none(self, supervisor):
        r1 = make_agent_result(should_notify=False)
        merged = supervisor._merge_results([r1], {})
        assert merged.comment_body is None


# ---------------------------------------------------------------------------
# handle() — full dispatch flow
# ---------------------------------------------------------------------------

class TestHandle:
    async def test_no_matching_agents(self, supervisor):
        ctx = make_agent_context(event_type="unknown.event")
        result = await supervisor.handle(ctx)

        assert result.status == "success"
        assert "No agents dispatched" in result.data.get("message", "")

    @patch("src.agents.supervisor.registry")
    @patch("src.agents.supervisor.async_session")
    async def test_single_agent_dispatch(self, mock_session, mock_registry, supervisor):
        # Mock the agent
        mock_agent = AsyncMock()
        mock_agent.handle = AsyncMock(return_value=make_agent_result(
            agent_name="issue_analyst", status="success",
        ))
        mock_agent._usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "by_model": {}}
        mock_registry.get.return_value = mock_agent

        # Mock DB session for _log_agent_start
        session = AsyncMock()
        mock_run = MagicMock()
        mock_run.id = 1
        session.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, 'id', 1))
        ctx_mgr = AsyncMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=session)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = ctx_mgr

        ctx = make_agent_context(event_type="issues.opened", repo_id=0)
        result = await supervisor.handle(ctx)

        assert result.status == "success"
        mock_agent.handle.assert_called_once()

    @patch("src.agents.supervisor.registry")
    @patch("src.agents.supervisor.async_session")
    async def test_agent_not_found(self, mock_session, mock_registry, supervisor):
        mock_registry.get.return_value = None

        session = AsyncMock()
        ctx_mgr = AsyncMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=session)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = ctx_mgr

        ctx = make_agent_context(event_type="issues.opened", repo_id=0)
        result = await supervisor.handle(ctx)

        assert result.data["agent_results"]["issue_analyst"]["status"] == "failed"

    @patch("src.agents.supervisor._get_blast_radius")
    @patch("src.agents.supervisor._get_pr_files")
    @patch("src.agents.supervisor._get_pr_diff")
    @patch("src.agents.supervisor.registry")
    @patch("src.agents.supervisor.async_session")
    async def test_pr_pre_gather(
        self, mock_session, mock_registry, mock_diff, mock_files, mock_blast, supervisor
    ):
        """PR events with multiple agents trigger pre-gathering."""
        mock_agent = AsyncMock()
        mock_agent.handle = AsyncMock(return_value=make_agent_result(status="success"))
        mock_agent._usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "by_model": {}}
        mock_registry.get.return_value = mock_agent

        # Mock DB session — cooldown check must return no recent runs
        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None  # No cooldown
        session.execute = AsyncMock(return_value=result_mock)
        ctx_mgr = AsyncMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=session)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = ctx_mgr

        mock_files.return_value = ToolResult(success=True, data=[{"filename": "src/main.py"}])
        mock_diff.return_value = ToolResult(success=True, data={"diff": "+added line"})
        mock_blast.return_value = ToolResult(success=True, data={"affected": 3})

        ctx = make_agent_context(
            event_type="pull_request.opened",
            event_payload={"pull_request": {"number": 5}},
            repo_id=42,
        )
        await supervisor.handle(ctx)

        # Pre-gather should have been called
        mock_files.assert_called_once()
        mock_diff.assert_called_once()

    @patch("src.agents.supervisor.registry")
    @patch("src.agents.supervisor.async_session")
    async def test_duration_ms_tracked(self, mock_session, mock_registry, supervisor):
        """Duration is tracked when agents are dispatched."""
        mock_agent = AsyncMock()
        mock_agent.handle = AsyncMock(return_value=make_agent_result(status="success"))
        mock_agent._usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "by_model": {}}
        mock_registry.get.return_value = mock_agent

        session = AsyncMock()
        ctx_mgr = AsyncMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=session)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = ctx_mgr

        ctx = make_agent_context(event_type="issues.opened", repo_id=0)
        result = await supervisor.handle(ctx)
        assert "duration_ms" in result.data


# ---------------------------------------------------------------------------
# _check_cooldown
# ---------------------------------------------------------------------------

class TestCheckCooldown:
    @patch("src.agents.supervisor.async_session")
    async def test_no_cooldown_returns_empty(self, mock_session, supervisor):
        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result_mock)

        ctx_mgr = AsyncMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=session)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = ctx_mgr

        cooled = await supervisor._check_cooldown(42, 1, ["issue_analyst"])
        assert cooled == []

    @patch("src.agents.supervisor.async_session")
    async def test_cooldown_active_returns_agent_name(self, mock_session, supervisor):
        session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = MagicMock()  # Found a recent run
        session.execute = AsyncMock(return_value=result_mock)

        ctx_mgr = AsyncMock()
        ctx_mgr.__aenter__ = AsyncMock(return_value=session)
        ctx_mgr.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = ctx_mgr

        cooled = await supervisor._check_cooldown(42, 1, ["issue_analyst"])
        assert "issue_analyst" in cooled
