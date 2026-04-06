"""
Gap-fill tests — covers remaining uncovered lines across multiple modules.
Each section targets a specific file's missed statements.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from src.tools.base import ToolResult


# ---------------------------------------------------------------------------
# src/tools/ai/predictor.py — _predict_completion (lines 80-114)
# ---------------------------------------------------------------------------

class TestPredictCompletion:
    @patch("src.tools.ai.predictor._client")
    async def test_success(self, mock_client):
        from src.tools.ai.predictor import _predict_completion

        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = json.dumps({
            "on_track": True, "risk_level": "low", "reasoning": "Good velocity",
        })
        response.usage = MagicMock(prompt_tokens=50, completion_tokens=30)
        mock_client.chat.completions.create = AsyncMock(return_value=response)

        result = await _predict_completion(
            {"velocity": 2.0, "closed_count": 5},
            {"title": "v1", "due_on": "2026-12-01"},
        )
        assert result.success is True
        assert result.data["on_track"] is True

    @patch("src.tools.ai.predictor._client")
    async def test_error(self, mock_client):
        from src.tools.ai.predictor import _predict_completion

        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("timeout"))
        result = await _predict_completion({}, {})
        assert result.success is False

    def test_make_predict_completion_factory(self):
        from src.tools.ai.predictor import make_predict_completion
        tool = make_predict_completion()
        assert tool.name == "predict_completion"

    def test_make_detect_blockers_factory(self):
        from src.tools.ai.predictor import make_detect_blockers
        tool = make_detect_blockers()
        assert tool.name == "detect_blockers"

    def test_make_detect_stale_prs_factory(self):
        from src.tools.ai.predictor import make_detect_stale_prs
        tool = make_detect_stale_prs()
        assert tool.name == "detect_stale_prs"


# ---------------------------------------------------------------------------
# src/tools/github/pull_requests.py — _persist_pr_file_changes (lines 86-122)
# ---------------------------------------------------------------------------

class TestPersistPrFileChanges:
    @patch("src.tools.github.pull_requests.async_session")
    async def test_no_pr_found_returns_early(self, mock_factory):
        from src.tools.github.pull_requests import _persist_pr_file_changes

        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None)
        ))
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        await _persist_pr_file_changes(42, 10, [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0}])
        session.commit.assert_not_called()

    @patch("src.tools.github.pull_requests.async_session")
    async def test_inserts_new_records(self, mock_factory):
        from src.tools.github.pull_requests import _persist_pr_file_changes

        session = AsyncMock()
        # First execute: find pr_id
        # Second execute: check existing file change
        call_count = [0]
        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(scalar_one_or_none=MagicMock(return_value=100))  # pr_id
            return MagicMock(scalar_one_or_none=MagicMock(return_value=None))  # no existing
        session.execute = mock_execute
        session.add = MagicMock()
        session.commit = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        await _persist_pr_file_changes(42, 10, [
            {"filename": "a.py", "status": "added", "additions": 5, "deletions": 0},
        ])
        session.add.assert_called_once()
        session.commit.assert_called_once()

    @patch("src.tools.github.pull_requests.async_session")
    async def test_updates_existing_records(self, mock_factory):
        from src.tools.github.pull_requests import _persist_pr_file_changes

        existing_record = MagicMock()
        session = AsyncMock()
        call_count = [0]
        async def mock_execute(stmt):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(scalar_one_or_none=MagicMock(return_value=100))
            return MagicMock(scalar_one_or_none=MagicMock(return_value=existing_record))
        session.execute = mock_execute
        session.commit = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        await _persist_pr_file_changes(42, 10, [
            {"filename": "a.py", "status": "modified", "additions": 3, "deletions": 1},
        ])
        assert existing_record.change_type == "modified"
        assert existing_record.additions == 3


# ---------------------------------------------------------------------------
# src/agents/supervisor.py — _log_agent_start, _log_agent_complete (lines 260-286)
# ---------------------------------------------------------------------------

class TestSupervisorLogging:
    @patch("src.agents.supervisor.async_session")
    async def test_log_agent_start_with_repo_id(self, mock_factory):
        from src.agents.supervisor import SupervisorAgent
        from tests.conftest import make_agent_context

        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock(side_effect=lambda obj: setattr(obj, 'id', 99))
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        sup = SupervisorAgent()
        context = make_agent_context(repo_id=42)
        run_id = await sup._log_agent_start("issue_analyst", context)
        session.add.assert_called_once()

    @patch("src.agents.supervisor.async_session")
    async def test_log_agent_start_no_repo_id(self, mock_factory):
        from src.agents.supervisor import SupervisorAgent
        from tests.conftest import make_agent_context

        sup = SupervisorAgent()
        context = make_agent_context(repo_id=0)
        run_id = await sup._log_agent_start("issue_analyst", context)
        assert run_id is None

    @patch("src.agents.supervisor.async_session")
    async def test_log_agent_complete(self, mock_factory):
        from src.agents.supervisor import SupervisorAgent
        from tests.conftest import make_agent_result
        import time

        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        sup = SupervisorAgent()
        result = make_agent_result(status="success")
        await sup._log_agent_complete(1, result, time.time() - 1)
        session.execute.assert_called_once()

    @patch("src.agents.supervisor.async_session")
    async def test_log_agent_complete_no_run_id(self, mock_factory):
        from src.agents.supervisor import SupervisorAgent
        from tests.conftest import make_agent_result
        import time

        sup = SupervisorAgent()
        result = make_agent_result(status="success")
        await sup._log_agent_complete(None, result, time.time())
        # Should return early without DB call
        mock_factory.assert_not_called()


# ---------------------------------------------------------------------------
# src/workers/tasks.py — installation_repositories branch (lines 70-91)
# ---------------------------------------------------------------------------

class TestTasksInstallationRepositories:
    @patch("src.workers.tasks._get_supervisor")
    @patch("src.workers.tasks.upsert_repository", new_callable=AsyncMock)
    async def test_installation_repositories_added(self, mock_upsert, mock_get_sup):
        from src.workers.tasks import dispatch_event
        from src.agents.base import AgentResult

        mock_upsert.return_value = 1
        mock_sup = AsyncMock()
        mock_sup.handle = AsyncMock(return_value=AgentResult(
            agent_name="supervisor", status="success", data={"agent_results": {}},
        ))
        mock_get_sup.return_value = mock_sup

        payload = {
            "repositories_added": [
                {"id": 100, "full_name": "owner/repo-a"},
            ],
        }
        await dispatch_event("installation_repositories", "added", "", 1001, payload)
        mock_upsert.assert_called_once()
        mock_sup.handle.assert_called_once()


# ---------------------------------------------------------------------------
# src/workers/context_updater.py — process_context_update (lines 75-87)
# ---------------------------------------------------------------------------

class TestProcessContextUpdate:
    @patch("src.workers.context_updater.update_context_on_push", new_callable=AsyncMock)
    @patch("src.workers.context_updater.upsert_repository", new_callable=AsyncMock)
    async def test_success(self, mock_upsert, mock_update):
        from src.workers.context_updater import process_context_update

        mock_upsert.return_value = 42
        mock_update.return_value = {"status": "success"}

        payload = {"repository": {"id": 999}}
        await process_context_update({}, "owner/repo", 1001, payload)
        mock_update.assert_called_once()

    @patch("src.workers.context_updater.update_context_on_push", new_callable=AsyncMock)
    @patch("src.workers.context_updater.upsert_repository", new_callable=AsyncMock)
    async def test_no_repo_id(self, mock_upsert, mock_update):
        from src.workers.context_updater import process_context_update

        payload = {}  # no repository key
        await process_context_update({}, "owner/repo", 1001, payload)
        mock_update.assert_not_called()
