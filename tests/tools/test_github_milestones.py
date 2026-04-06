"""Tests for src.tools.github.milestones — milestone CRUD."""

from unittest.mock import AsyncMock, patch

from src.tools.github.milestones import (
    _get_all_milestones, _get_milestone, _create_milestone, _update_milestone,
    make_get_all_milestones, make_get_milestone, make_create_milestone, make_update_milestone,
)


class TestGetAllMilestones:
    @patch("src.tools.github.milestones.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(return_value=[{"number": 1, "title": "v1"}])
        result = await _get_all_milestones(1001, "owner/repo")
        assert result.success is True
        assert len(result.data) == 1

    @patch("src.tools.github.milestones.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(side_effect=Exception("err"))
        result = await _get_all_milestones(1001, "owner/repo")
        assert result.success is False


class TestGetMilestone:
    @patch("src.tools.github.milestones.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(return_value={"number": 1, "title": "v1"})
        result = await _get_milestone(1001, "owner/repo", 1)
        assert result.success is True

    @patch("src.tools.github.milestones.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(side_effect=Exception("404"))
        result = await _get_milestone(1001, "owner/repo", 999)
        assert result.success is False


class TestCreateMilestone:
    @patch("src.tools.github.milestones.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(return_value={"number": 2, "title": "v2"})
        result = await _create_milestone(1001, "owner/repo", "v2")
        assert result.success is True

    @patch("src.tools.github.milestones.GitHubClient")
    async def test_with_due_date(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(return_value={"number": 2})
        result = await _create_milestone(1001, "owner/repo", "v2", due_on="2026-12-01T00:00:00Z")
        assert result.success is True
        payload = mock_cls.return_value.post.call_args[1]["json"]
        assert payload["due_on"] == "2026-12-01T00:00:00Z"

    @patch("src.tools.github.milestones.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(side_effect=Exception("err"))
        result = await _create_milestone(1001, "owner/repo", "v2")
        assert result.success is False


class TestUpdateMilestone:
    @patch("src.tools.github.milestones.GitHubClient")
    async def test_partial_update(self, mock_cls):
        mock_cls.return_value.patch = AsyncMock(return_value={"number": 1})
        result = await _update_milestone(1001, "owner/repo", 1, state="closed")
        assert result.success is True
        payload = mock_cls.return_value.patch.call_args[1]["json"]
        assert payload == {"state": "closed"}

    @patch("src.tools.github.milestones.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.patch = AsyncMock(side_effect=Exception("err"))
        result = await _update_milestone(1001, "owner/repo", 1, title="new")
        assert result.success is False


class TestFactories:
    def test_make_get_all_milestones(self):
        assert make_get_all_milestones(1, "o/r").name == "get_all_milestones"

    def test_make_get_milestone(self):
        assert make_get_milestone(1, "o/r").name == "get_milestone"

    def test_make_create_milestone(self):
        assert make_create_milestone(1, "o/r").name == "create_milestone"

    def test_make_update_milestone(self):
        assert make_update_milestone(1, "o/r").name == "update_milestone"
