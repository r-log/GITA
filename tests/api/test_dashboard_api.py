"""Tests for src.api.dashboard_api — dashboard endpoints."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from src.api.dashboard_api import (
    list_repos, get_stats, list_runs, get_run,
    list_agent_runs, get_agent_run, list_analyses,
    get_activity, get_issues_from_plan, get_alerts, get_costs,
    trigger_action, _serialize_datetime,
)


def _mock_session():
    """Create a mock async session + context manager."""
    session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return session, ctx


def _mock_repo(id=1, full_name="owner/repo", github_id=999, installation_id=1001):
    r = MagicMock()
    r.id = id
    r.full_name = full_name
    r.github_id = github_id
    r.installation_id = installation_id
    r.created_at = None
    return r


def _mock_onboarding_run(id=1, status="success"):
    r = MagicMock()
    r.id = id
    r.repo_id = 1
    r.status = status
    r.repo_snapshot = {}
    r.suggested_plan = {"milestones": []}
    r.existing_state = {}
    r.actions_taken = []
    r.issues_created = 3
    r.issues_updated = 0
    r.milestones_created = 1
    r.milestones_updated = 0
    r.confidence = 0.9
    r.started_at = None
    r.completed_at = None
    return r


def _mock_agent_run(id=1, agent_name="issue_analyst", status="success"):
    r = MagicMock()
    r.id = id
    r.agent_name = agent_name
    r.event_type = "issues.opened"
    r.status = status
    r.confidence = 0.8
    r.duration_ms = 1200
    r.error_message = None
    r.context = {}
    r.tools_called = [{"tool": "get_issue"}]
    r.result = {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "llm_calls": 1, "by_model": {}}}
    r.started_at = datetime(2026, 1, 1, 12, 0, 0)
    r.completed_at = datetime(2026, 1, 1, 12, 0, 1)
    return r


def _mock_analysis(id=1, risk_level="warning"):
    a = MagicMock()
    a.id = id
    a.target_type = "issue"
    a.target_number = 5
    a.analysis_type = "smart_eval"
    a.score = 7.5
    a.risk_level = risk_level
    a.result = {"findings": {"critical": [], "warning": [], "info": []}}
    a.created_at = datetime(2026, 1, 1)
    return a


# ---------------------------------------------------------------------------

class TestSerializeDatetime:
    def test_with_datetime(self):
        dt = datetime(2026, 1, 1, 12, 0, 0)
        assert _serialize_datetime(dt) == "2026-01-01T12:00:00"

    def test_with_none(self):
        assert _serialize_datetime(None) is None


class TestListRepos:
    @patch("src.api.dashboard_api.async_session")
    async def test_returns_repos(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[_mock_repo()])))
        )
        result = await list_repos()
        assert len(result) == 1
        assert result[0]["full_name"] == "owner/repo"

    @patch("src.api.dashboard_api.async_session")
    async def test_empty_repos(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )
        result = await list_repos()
        assert result == []


class TestGetStats:
    @patch("src.api.dashboard_api.async_session")
    async def test_returns_stats(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx

        # Need to return different results for each execute() call
        onboarding_row = MagicMock()
        onboarding_row.total = 2
        onboarding_row.last_completed = None

        call_count = [0]
        async def mock_execute(stmt):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] == 1:  # onboarding counts
                result.one.return_value = onboarding_row
            elif call_count[0] == 2:  # last context update
                result.scalar_one_or_none.return_value = None
            elif call_count[0] == 3:  # agent counts by name
                result.all.return_value = [("issue_analyst", 5)]
            elif call_count[0] == 4:  # status counts
                result.all.return_value = [("success", 4), ("failed", 1)]
            elif call_count[0] == 5:  # plan
                result.scalar_one_or_none.return_value = None
            return result

        session.execute = mock_execute
        result = await get_stats(repo_id=1)
        assert result["total_onboarding_runs"] == 2
        assert result["total_agent_runs"] == 5
        assert "agent_run_counts" in result


class TestListRuns:
    @patch("src.api.dashboard_api.async_session")
    async def test_returns_runs(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[_mock_onboarding_run()])))
        )
        result = await list_runs(repo_id=1, status=None, limit=50)
        assert len(result) == 1
        assert result[0]["status"] == "success"

    @patch("src.api.dashboard_api.async_session")
    async def test_with_status_filter(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )
        result = await list_runs(repo_id=1, status="failed", limit=10)
        assert result == []


class TestGetRun:
    @patch("src.api.dashboard_api.async_session")
    async def test_found(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=_mock_onboarding_run())
        )
        result = await get_run(run_id=1)
        assert result["id"] == 1
        assert result["status"] == "success"

    @patch("src.api.dashboard_api.async_session")
    async def test_not_found(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=None)
        )
        result = await get_run(run_id=999)
        assert result["error"] == "Run not found"


class TestListAgentRuns:
    @patch("src.api.dashboard_api.async_session")
    async def test_returns_list(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[_mock_agent_run()])))
        )
        result = await list_agent_runs(repo_id=1, agent_name=None, status=None, limit=50)
        assert len(result) == 1
        assert result[0]["agent_name"] == "issue_analyst"

    @patch("src.api.dashboard_api.async_session")
    async def test_with_filters(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )
        result = await list_agent_runs(repo_id=1, agent_name="pr_reviewer", status="failed", limit=10)
        assert result == []


class TestGetAgentRun:
    @patch("src.api.dashboard_api.async_session")
    async def test_found(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=_mock_agent_run())
        )
        result = await get_agent_run(run_id=1)
        assert result["id"] == 1
        assert result["agent_name"] == "issue_analyst"

    @patch("src.api.dashboard_api.async_session")
    async def test_not_found(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=None)
        )
        result = await get_agent_run(run_id=999)
        assert result["error"] == "Agent run not found"


class TestListAnalyses:
    @patch("src.api.dashboard_api.async_session")
    async def test_returns_list(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[_mock_analysis()])))
        )
        result = await list_analyses(repo_id=1, analysis_type=None, limit=20)
        assert len(result) == 1
        assert result[0]["analysis_type"] == "smart_eval"

    @patch("src.api.dashboard_api.async_session")
    async def test_with_type_filter(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )
        result = await list_analyses(repo_id=1, analysis_type="risk_scan", limit=10)
        assert result == []


class TestGetActivity:
    @patch("src.api.dashboard_api.async_session")
    async def test_returns_timeline(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        row = MagicMock()
        row.date = "2026-01-01"
        row.total = 5
        row.success = 4
        row.failed = 1
        session.execute.return_value = MagicMock(all=MagicMock(return_value=[row]))

        result = await get_activity(repo_id=1, days=30)
        assert len(result) == 1
        assert result[0]["total"] == 5

    @patch("src.api.dashboard_api.async_session")
    async def test_empty_activity(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(all=MagicMock(return_value=[]))
        result = await get_activity(repo_id=1, days=7)
        assert result == []


class TestGetIssuesFromPlan:
    @patch("src.api.dashboard_api.async_session")
    async def test_with_plan(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        plan = {
            "milestones": [{
                "title": "v1", "description": "First", "confidence": 0.9,
                "tasks": [
                    {"title": "Add auth", "status": "done", "effort": "M", "labels": ["enhancement"], "files": ["auth.py"]},
                    {"title": "Add DB", "status": "not-started", "effort": "L", "labels": [], "files": []},
                ],
            }]
        }
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=plan))

        result = await get_issues_from_plan(repo_id=1)
        assert len(result["milestones"]) == 1
        assert result["milestones"][0]["total_tasks"] == 2
        assert result["milestones"][0]["done_tasks"] == 1
        assert result["milestones"][0]["progress_pct"] == 50

    @patch("src.api.dashboard_api.async_session")
    async def test_no_plan(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        result = await get_issues_from_plan(repo_id=1)
        assert result == {"milestones": []}


class TestGetAlerts:
    @patch("src.api.dashboard_api.async_session")
    async def test_no_alerts(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx

        call_count = [0]
        async def mock_execute(stmt):
            call_count[0] += 1
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result
        session.execute = mock_execute

        result = await get_alerts(repo_id=1)
        assert result["total"] == 0
        assert result["critical"] == []
        assert result["warnings"] == []

    @patch("src.api.dashboard_api.async_session")
    async def test_with_failed_agent_runs(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx

        failed_run = _mock_agent_run(status="failed")
        failed_run.error_message = "Timeout"

        call_count = [0]
        async def mock_execute(stmt):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] == 1:  # analyses
                result.scalars.return_value.all.return_value = []
            else:  # failed runs
                result.scalars.return_value.all.return_value = [failed_run]
            return result
        session.execute = mock_execute

        result = await get_alerts(repo_id=1)
        assert result["total"] == 1
        assert len(result["warnings"]) == 1
        assert "Timeout" in result["warnings"][0]["message"]


class TestGetCosts:
    @patch("src.api.dashboard_api.async_session")
    async def test_with_runs(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx

        run = _mock_agent_run()
        # Return tuples: (agent_name, result, started_at)
        row = (run.agent_name, run.result, run.started_at)
        session.execute.return_value = MagicMock(all=MagicMock(return_value=[row]))

        result = await get_costs(repo_id=1, days=30)
        assert result["total_prompt_tokens"] == 100
        assert result["total_completion_tokens"] == 50
        assert result["total_llm_calls"] == 1
        assert "by_agent" in result
        assert "issue_analyst" in result["by_agent"]

    @patch("src.api.dashboard_api.async_session")
    async def test_empty_runs(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(all=MagicMock(return_value=[]))

        result = await get_costs(repo_id=1, days=30)
        assert result["total_prompt_tokens"] == 0
        assert result["total_cost_usd"] == 0


class TestTriggerAction:
    @patch("src.api.dashboard_api.async_session")
    async def test_missing_repo_id(self, mock_factory):
        request = AsyncMock()
        request.body = AsyncMock(return_value=b'{"action": "reconcile"}')
        result = await trigger_action(request)
        assert result["status"] == "error"
        assert "repo_id required" in result["message"]

    @patch("src.api.dashboard_api.async_session")
    async def test_repo_not_found(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=None)
        )

        request = AsyncMock()
        request.body = AsyncMock(return_value=b'{"repo_id": 999, "action": "reconcile"}')
        result = await trigger_action(request)
        assert result["status"] == "error"
        assert "not found" in result["message"]

    @patch("src.api.dashboard_api.async_session")
    async def test_reconcile_action(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=_mock_repo())
        )

        with patch("src.workers.reconciliation.reconcile_repo", new_callable=AsyncMock) as mock_reconcile:
            mock_reconcile.return_value = {"status": "success"}
            request = AsyncMock()
            request.body = AsyncMock(return_value=b'{"repo_id": 1, "action": "reconcile"}')
            result = await trigger_action(request)

        assert result["status"] == "ok"
        assert result["action"] == "reconcile"

    @patch("src.api.dashboard_api.async_session")
    async def test_unknown_action(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=_mock_repo())
        )

        request = AsyncMock()
        request.body = AsyncMock(return_value=b'{"repo_id": 1, "action": "unknown"}')
        result = await trigger_action(request)
        assert result["status"] == "error"
        assert "Unknown action" in result["message"]

    async def test_invalid_json(self):
        request = AsyncMock()
        request.body = AsyncMock(return_value=b"not json {{{")
        result = await trigger_action(request)
        assert result["status"] == "error"
        assert "Invalid JSON" in result["message"]
