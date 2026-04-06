"""Tests for src.tools.github.issues — CRUD and factories."""

import pytest
from unittest.mock import AsyncMock, patch

from src.tools.github.issues import (
    _get_issue, _get_all_issues, _create_issue, _update_issue,
    make_get_issue, make_get_all_issues, make_create_issue, make_update_issue,
)


@pytest.fixture
def mock_client():
    client = AsyncMock()
    return client


class TestGetIssue:
    @patch("src.tools.github.issues.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(return_value={"number": 1, "title": "Bug"})

        result = await _get_issue(1001, "owner/repo", 1)
        assert result.success is True
        assert result.data["number"] == 1

    @patch("src.tools.github.issues.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(side_effect=Exception("404"))

        result = await _get_issue(1001, "owner/repo", 999)
        assert result.success is False
        assert "404" in result.error


class TestGetAllIssues:
    @patch("src.tools.github.issues.GitHubClient")
    async def test_single_page(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(return_value=[
            {"number": 1, "title": "Issue 1"},
            {"number": 2, "title": "Issue 2"},
        ])

        result = await _get_all_issues(1001, "owner/repo")
        assert result.success is True
        assert len(result.data) == 2

    @patch("src.tools.github.issues.GitHubClient")
    async def test_filters_out_pull_requests(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(return_value=[
            {"number": 1, "title": "Issue"},
            {"number": 2, "title": "PR", "pull_request": {"url": "..."}},
        ])

        result = await _get_all_issues(1001, "owner/repo")
        assert len(result.data) == 1
        assert result.data[0]["number"] == 1

    @patch("src.tools.github.issues.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(side_effect=Exception("timeout"))

        result = await _get_all_issues(1001, "owner/repo")
        assert result.success is False


class TestCreateIssue:
    @patch("src.tools.github.issues.GitHubClient")
    async def test_minimal_payload(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(return_value={"number": 10, "title": "New"})

        result = await _create_issue(1001, "owner/repo", "New")
        assert result.success is True
        assert result.data["number"] == 10

    @patch("src.tools.github.issues.GitHubClient")
    async def test_full_payload(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(return_value={"number": 10})

        result = await _create_issue(
            1001, "owner/repo", "Title", body="Body",
            labels=["bug"], milestone=1, assignees=["dev"],
        )
        assert result.success is True
        # Verify the payload sent
        call_kwargs = mock_cls.return_value.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        assert payload["title"] == "Title"

    @patch("src.tools.github.issues.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(side_effect=Exception("rate limit"))

        result = await _create_issue(1001, "owner/repo", "New")
        assert result.success is False


class TestUpdateIssue:
    @patch("src.tools.github.issues.GitHubClient")
    async def test_partial_update(self, mock_cls):
        mock_cls.return_value.patch = AsyncMock(return_value={"number": 1, "state": "closed"})

        result = await _update_issue(1001, "owner/repo", 1, state="closed")
        assert result.success is True

    @patch("src.tools.github.issues.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.patch = AsyncMock(side_effect=Exception("forbidden"))

        result = await _update_issue(1001, "owner/repo", 1, title="new title")
        assert result.success is False


class TestFactories:
    def test_make_get_issue(self):
        tool = make_get_issue(1001, "owner/repo")
        assert tool.name == "get_issue"

    def test_make_get_all_issues(self):
        tool = make_get_all_issues(1001, "owner/repo")
        assert tool.name == "get_all_issues"

    def test_make_create_issue(self):
        tool = make_create_issue(1001, "owner/repo")
        assert tool.name == "create_issue"

    def test_make_update_issue(self):
        tool = make_update_issue(1001, "owner/repo")
        assert tool.name == "update_issue"
