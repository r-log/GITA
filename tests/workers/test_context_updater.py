"""Tests for src.workers.context_updater — push event context updates."""

from unittest.mock import AsyncMock, patch

from src.workers.context_updater import _extract_changed_files, update_context_on_push


class TestExtractChangedFiles:
    def test_added_and_modified(self):
        payload = {
            "commits": [
                {"added": ["new.py"], "modified": ["old.py"], "removed": []},
                {"added": [], "modified": ["old.py"], "removed": []},
            ]
        }
        changed, removed = _extract_changed_files(payload)
        assert "new.py" in changed
        assert "old.py" in changed
        assert len(removed) == 0

    def test_removed_files(self):
        payload = {
            "commits": [
                {"added": [], "modified": [], "removed": ["deleted.py"]},
            ]
        }
        changed, removed = _extract_changed_files(payload)
        assert "deleted.py" in removed

    def test_dedup_across_commits(self):
        payload = {
            "commits": [
                {"added": ["file.py"], "modified": [], "removed": []},
                {"added": ["file.py"], "modified": [], "removed": []},
            ]
        }
        changed, removed = _extract_changed_files(payload)
        assert len(changed) == 1

    def test_empty_commits(self):
        payload = {"commits": []}
        changed, removed = _extract_changed_files(payload)
        assert len(changed) == 0
        assert len(removed) == 0


class TestUpdateContextOnPush:
    @patch("src.workers.context_updater.reindex_files", new_callable=AsyncMock)
    async def test_no_changes_returns_skipped(self, mock_reindex):
        payload = {"commits": [{"added": [], "modified": [], "removed": []}]}
        result = await update_context_on_push(42, "owner/repo", 1001, payload)
        assert result["status"] == "skipped"
        mock_reindex.assert_not_called()

    @patch("src.workers.context_updater.reindex_files", new_callable=AsyncMock)
    async def test_with_changes_calls_reindex(self, mock_reindex):
        mock_reindex.return_value = {"files_updated": 1, "files_removed": 0}
        payload = {"commits": [{"added": ["new.py"], "modified": [], "removed": []}]}
        result = await update_context_on_push(42, "owner/repo", 1001, payload)
        assert result["status"] == "success"
        mock_reindex.assert_called_once()
