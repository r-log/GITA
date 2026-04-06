"""Tests for src.tools.github.pull_requests — PR data gathering."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.tools.github.pull_requests import (
    _get_pr, _get_pr_diff, _get_pr_files, _get_open_prs,
    make_get_pr, make_get_open_prs,
)


class TestGetPr:
    @patch("src.tools.github.pull_requests.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(return_value={"number": 10, "title": "Fix bug"})

        result = await _get_pr(1001, "owner/repo", 10)
        assert result.success is True
        assert result.data["number"] == 10

    @patch("src.tools.github.pull_requests.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(side_effect=Exception("not found"))

        result = await _get_pr(1001, "owner/repo", 999)
        assert result.success is False


class TestGetPrDiff:
    @patch("httpx.AsyncClient")
    @patch("src.tools.github.pull_requests.GitHubClient")
    async def test_success(self, mock_gh_cls, mock_httpx_cls):
        mock_gh_cls.return_value._ensure_token = AsyncMock(return_value="fake-token")

        mock_response = MagicMock()
        mock_response.text = "+added line\n-removed line"
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_cls.return_value = mock_http

        result = await _get_pr_diff(1001, "owner/repo", 10)
        assert result.success is True
        assert "+added line" in result.data["diff"]

    @patch("httpx.AsyncClient")
    @patch("src.tools.github.pull_requests.GitHubClient")
    async def test_large_diff_truncated(self, mock_gh_cls, mock_httpx_cls):
        mock_gh_cls.return_value._ensure_token = AsyncMock(return_value="fake-token")

        mock_response = MagicMock()
        mock_response.text = "x" * 60000
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_cls.return_value = mock_http

        result = await _get_pr_diff(1001, "owner/repo", 10)
        assert result.success is True
        assert "truncated" in result.data["diff"]
        assert result.data["size"] == 60000

    @patch("src.tools.github.pull_requests.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value._ensure_token = AsyncMock(side_effect=Exception("timeout"))

        result = await _get_pr_diff(1001, "owner/repo", 10)
        assert result.success is False


class TestGetPrFiles:
    @patch("src.tools.github.pull_requests._persist_pr_file_changes", new_callable=AsyncMock)
    @patch("src.tools.github.pull_requests.GitHubClient")
    async def test_success(self, mock_cls, mock_persist):
        mock_cls.return_value.get = AsyncMock(return_value=[
            {"filename": "src/main.py", "status": "modified", "additions": 5, "deletions": 2, "changes": 7},
        ])

        result = await _get_pr_files(1001, "owner/repo", 10, repo_id=42)
        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0]["filename"] == "src/main.py"

    @patch("src.tools.github.pull_requests._persist_pr_file_changes", new_callable=AsyncMock)
    @patch("src.tools.github.pull_requests.GitHubClient")
    async def test_persist_called_with_repo_id(self, mock_cls, mock_persist):
        mock_cls.return_value.get = AsyncMock(return_value=[
            {"filename": "a.py", "status": "added", "additions": 1, "deletions": 0, "changes": 1},
        ])

        await _get_pr_files(1001, "owner/repo", 10, repo_id=42)
        mock_persist.assert_called_once()

    @patch("src.tools.github.pull_requests._persist_pr_file_changes", new_callable=AsyncMock)
    @patch("src.tools.github.pull_requests.GitHubClient")
    async def test_persist_skipped_without_repo_id(self, mock_cls, mock_persist):
        mock_cls.return_value.get = AsyncMock(return_value=[])

        await _get_pr_files(1001, "owner/repo", 10, repo_id=0)
        mock_persist.assert_not_called()


class TestGetOpenPrs:
    @patch("src.tools.github.pull_requests.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(return_value=[
            {"number": 1, "title": "PR 1"},
            {"number": 2, "title": "PR 2"},
        ])

        result = await _get_open_prs(1001, "owner/repo")
        assert result.success is True
        assert len(result.data) == 2


class TestFactories:
    def test_make_get_pr(self):
        tool = make_get_pr(1001, "owner/repo")
        assert tool.name == "get_pr"

    def test_make_get_open_prs(self):
        tool = make_get_open_prs(1001, "owner/repo")
        assert tool.name == "get_open_prs"
