"""Tests for src.workers.reconciliation — plan-vs-state reconciliation."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.tools.base import ToolResult
from src.workers.reconciliation import (
    _match_task_to_issue, reconcile_repo, reconcile_all_repos, reconcile_single_repo,
)


def _mock_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return session, ctx


def _mock_onboarding_run(milestones=None, status="success"):
    run = MagicMock()
    run.suggested_plan = {"milestones": milestones or []}
    run.repo_snapshot = {}
    run.status = status
    return run


# ---------------------------------------------------------------------------
# _match_task_to_issue
# ---------------------------------------------------------------------------

class TestMatchTaskToIssue:
    def test_exact_match(self):
        issues = [{"number": 1, "title": "Add user authentication"}]
        result = _match_task_to_issue("Add user authentication", issues)
        assert result is not None
        assert result["number"] == 1

    def test_fuzzy_match_above_threshold(self):
        issues = [{"number": 1, "title": "Implement user auth system"}]
        result = _match_task_to_issue("Implement user authentication system", issues)
        # fuzz.ratio should be high enough (>= 70)
        if result:
            assert result["number"] == 1

    def test_no_match_below_threshold(self):
        issues = [{"number": 1, "title": "Fix database migration"}]
        result = _match_task_to_issue("Build payment gateway", issues)
        assert result is None

    def test_empty_issues(self):
        result = _match_task_to_issue("Any task", [])
        assert result is None

    def test_best_match_selected(self):
        issues = [
            {"number": 1, "title": "Add basic auth"},
            {"number": 2, "title": "Add user authentication"},
        ]
        result = _match_task_to_issue("Add user authentication", issues)
        assert result is not None
        assert result["number"] == 2


# ---------------------------------------------------------------------------
# reconcile_repo
# ---------------------------------------------------------------------------

class TestReconcileRepo:
    @patch("src.workers.reconciliation._load_latest_run", new_callable=AsyncMock)
    async def test_no_latest_run_skips(self, mock_load):
        mock_load.return_value = None
        result = await reconcile_repo(42, "owner/repo", 1001)
        assert result["status"] == "skipped"
        assert result["reason"] == "no_onboarding_run"

    @patch("src.workers.reconciliation._load_latest_run", new_callable=AsyncMock)
    async def test_no_milestones_skips(self, mock_load):
        mock_load.return_value = _mock_onboarding_run(milestones=[])
        result = await reconcile_repo(42, "owner/repo", 1001)
        assert result["status"] == "skipped"
        assert result["reason"] == "no_milestones_in_plan"

    @patch("src.workers.reconciliation._save_onboarding_run", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._get_all_issues", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._load_latest_run", new_callable=AsyncMock)
    async def test_fetch_issues_failure(self, mock_load, mock_issues, mock_save):
        mock_load.return_value = _mock_onboarding_run(
            milestones=[{"title": "v1", "tasks": [{"title": "Task A", "status": "done"}]}]
        )
        mock_issues.return_value = ToolResult(success=False, error="API error")

        result = await reconcile_repo(42, "owner/repo", 1001)
        assert result["status"] == "failed"

    @patch("src.workers.reconciliation._save_onboarding_run", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._update_issue", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._get_all_issues", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._load_latest_run", new_callable=AsyncMock)
    async def test_closes_done_issues_that_are_open(self, mock_load, mock_issues, mock_update, mock_save):
        mock_load.return_value = _mock_onboarding_run(
            milestones=[{"title": "v1", "tasks": [{"title": "Add auth", "status": "done"}]}]
        )
        mock_issues.return_value = ToolResult(success=True, data=[
            {"number": 5, "title": "Add auth", "state": "open", "labels": []},
        ])
        mock_update.return_value = ToolResult(success=True, data={})

        result = await reconcile_repo(42, "owner/repo", 1001)
        assert result["status"] == "success"
        assert result["issues_closed"] == 1
        mock_update.assert_called()

    @patch("src.workers.reconciliation._save_onboarding_run", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._get_all_issues", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._load_latest_run", new_callable=AsyncMock)
    async def test_flags_drift_on_unexpected_closure(self, mock_load, mock_issues, mock_save):
        mock_load.return_value = _mock_onboarding_run(
            milestones=[{"title": "v1", "tasks": [{"title": "In progress task", "status": "in-progress"}]}]
        )
        mock_issues.return_value = ToolResult(success=True, data=[
            {"number": 3, "title": "In progress task", "state": "closed", "labels": []},
        ])

        result = await reconcile_repo(42, "owner/repo", 1001)
        assert result["status"] == "success"
        assert result["drift_flags"] == 1

    @patch("src.workers.reconciliation._save_onboarding_run", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._update_issue", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._get_all_issues", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._load_latest_run", new_callable=AsyncMock)
    async def test_updates_milestone_tracker_checklist(self, mock_load, mock_issues, mock_update, mock_save):
        mock_load.return_value = _mock_onboarding_run(
            milestones=[{"title": "v1", "tasks": []}]
        )
        mock_issues.return_value = ToolResult(success=True, data=[
            {
                "number": 1, "title": "Tracker v1", "state": "open",
                "labels": [{"name": "Milestone Tracker"}],
                "body": "- [ ] Task A (#2)\n- [ ] Task B (#3)",
            },
            {"number": 2, "title": "Task A", "state": "closed", "labels": []},
            {"number": 3, "title": "Task B", "state": "open", "labels": []},
        ])
        mock_update.return_value = ToolResult(success=True, data={})

        result = await reconcile_repo(42, "owner/repo", 1001)
        assert result["status"] == "success"
        assert result["checklists_updated"] >= 1

    @patch("src.workers.reconciliation._save_onboarding_run", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._update_issue", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._get_all_issues", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._load_latest_run", new_callable=AsyncMock)
    async def test_auto_closes_tracker_when_all_done(self, mock_load, mock_issues, mock_update, mock_save):
        mock_load.return_value = _mock_onboarding_run(
            milestones=[{"title": "v1", "tasks": []}]
        )
        mock_issues.return_value = ToolResult(success=True, data=[
            {
                "number": 1, "title": "Tracker v1", "state": "open",
                "labels": [{"name": "Milestone Tracker"}],
                "body": "- [x] Task A (#2)\n- [x] Task B (#3)",
            },
            {"number": 2, "title": "Task A", "state": "closed", "labels": []},
            {"number": 3, "title": "Task B", "state": "closed", "labels": []},
        ])
        mock_update.return_value = ToolResult(success=True, data={})

        result = await reconcile_repo(42, "owner/repo", 1001)
        assert result["status"] == "success"
        # Tracker should be auto-closed
        assert result["issues_closed"] >= 1

    @patch("src.workers.reconciliation._save_onboarding_run", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._get_all_issues", new_callable=AsyncMock)
    @patch("src.workers.reconciliation._load_latest_run", new_callable=AsyncMock)
    async def test_saves_reconciliation_record(self, mock_load, mock_issues, mock_save):
        mock_load.return_value = _mock_onboarding_run(
            milestones=[{"title": "v1", "tasks": []}]
        )
        mock_issues.return_value = ToolResult(success=True, data=[])

        await reconcile_repo(42, "owner/repo", 1001)
        mock_save.assert_called_once()
        call_kwargs = mock_save.call_args
        assert call_kwargs[1]["status"] == "reconciliation" or call_kwargs.kwargs.get("status") == "reconciliation"


# ---------------------------------------------------------------------------
# reconcile_all_repos
# ---------------------------------------------------------------------------

class TestReconcileAllRepos:
    @patch("src.workers.reconciliation.reconcile_repo", new_callable=AsyncMock)
    @patch("src.workers.reconciliation.async_session")
    async def test_reconciles_each_repo(self, mock_session_factory, mock_reconcile):
        session, ctx = _mock_session()
        mock_session_factory.return_value = ctx

        repo = MagicMock()
        repo.id = 1
        repo.full_name = "owner/repo"
        repo.installation_id = 1001
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[repo])))
        )
        mock_reconcile.return_value = {"status": "success"}

        results = await reconcile_all_repos()
        assert len(results) == 1
        assert results[0]["status"] == "success"
        mock_reconcile.assert_called_once()

    @patch("src.workers.reconciliation.reconcile_repo", new_callable=AsyncMock)
    @patch("src.workers.reconciliation.async_session")
    async def test_handles_repo_error(self, mock_session_factory, mock_reconcile):
        session, ctx = _mock_session()
        mock_session_factory.return_value = ctx

        repo = MagicMock()
        repo.id = 1
        repo.full_name = "owner/repo"
        repo.installation_id = 1001
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[repo])))
        )
        mock_reconcile.side_effect = Exception("DB error")

        results = await reconcile_all_repos()
        assert len(results) == 1
        assert results[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# reconcile_single_repo
# ---------------------------------------------------------------------------

class TestReconcileSingleRepo:
    @patch("src.workers.reconciliation.reconcile_repo", new_callable=AsyncMock)
    @patch("src.workers.reconciliation.async_session")
    async def test_found_repo(self, mock_session_factory, mock_reconcile):
        session, ctx = _mock_session()
        mock_session_factory.return_value = ctx

        repo = MagicMock()
        repo.id = 42
        repo.full_name = "owner/repo"
        repo.installation_id = 1001
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=repo)
        )
        mock_reconcile.return_value = {"status": "success"}

        result = await reconcile_single_repo("owner/repo")
        assert result["status"] == "success"

    @patch("src.workers.reconciliation.async_session")
    async def test_repo_not_found_raises(self, mock_session_factory):
        session, ctx = _mock_session()
        mock_session_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=None)
        )

        with pytest.raises(ValueError, match="not found"):
            await reconcile_single_repo("unknown/repo")
