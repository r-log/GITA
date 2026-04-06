"""Tests for src.workers.settings — ARQ worker config."""

from unittest.mock import AsyncMock, patch, MagicMock


class TestStartup:
    @patch("src.workers.settings.register_all_agents")
    async def test_calls_register_all_agents(self, mock_register):
        from src.workers.settings import startup
        await startup({})
        mock_register.assert_called_once()


class TestProcessWebhook:
    @patch("src.workers.settings.dispatch_event", new_callable=AsyncMock)
    async def test_dispatches_event(self, mock_dispatch):
        from src.workers.settings import process_webhook
        await process_webhook({}, "issues", "opened", "owner/repo", 1001, {"action": "opened"})
        mock_dispatch.assert_called_once_with("issues", "opened", "owner/repo", 1001, {"action": "opened"})


class TestWorkerSettings:
    def test_has_required_fields(self):
        from src.workers.settings import WorkerSettings
        assert hasattr(WorkerSettings, "functions")
        assert hasattr(WorkerSettings, "cron_jobs")
        assert WorkerSettings.max_jobs > 0
