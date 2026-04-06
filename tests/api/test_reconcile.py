"""Tests for src.api.reconcile — reconciliation trigger endpoint."""

import json
from unittest.mock import AsyncMock, patch, MagicMock

from src.api.reconcile import trigger_reconciliation


class TestTriggerReconciliation:
    @patch("src.api.reconcile.reconcile_all_repos", new_callable=AsyncMock, create=True)
    async def test_reconcile_all(self, mock_reconcile):
        mock_reconcile.return_value = [{"repo": "o/r", "status": "success"}]
        request = AsyncMock()
        request.body = AsyncMock(return_value=b"")

        # Need to patch the import inside the function
        with patch("src.workers.reconciliation.reconcile_all_repos", mock_reconcile):
            result = await trigger_reconciliation(request)

        assert result["status"] == "ok"

    async def test_reconcile_single(self):
        request = AsyncMock()
        request.body = AsyncMock(return_value=b'{"repo_full_name": "owner/repo"}')

        with patch("src.workers.reconciliation.reconcile_single_repo", new_callable=AsyncMock) as mock_single:
            mock_single.return_value = {"status": "success"}
            result = await trigger_reconciliation(request)

        assert result["status"] == "ok"

    async def test_repo_not_found(self):
        request = AsyncMock()
        request.body = AsyncMock(return_value=b'{"repo_full_name": "unknown/repo"}')

        with patch("src.workers.reconciliation.reconcile_single_repo", new_callable=AsyncMock) as mock_single:
            mock_single.side_effect = ValueError("not found")
            result = await trigger_reconciliation(request)

        assert result["status"] == "error"

    async def test_invalid_json(self):
        request = AsyncMock()
        request.body = AsyncMock(return_value=b"not json {{{")

        result = await trigger_reconciliation(request)
        assert result["status"] == "error"
        assert "Invalid JSON" in result["message"]
