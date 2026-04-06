"""Tests for src.tools.github.labels — label operations."""

from unittest.mock import AsyncMock, patch

from src.tools.github.labels import _add_label, _create_label, make_add_label, make_create_label


class TestAddLabel:
    @patch("src.tools.github.labels.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(return_value=[{"name": "bug"}])
        result = await _add_label(1001, "owner/repo", 1, ["bug"])
        assert result.success is True

    @patch("src.tools.github.labels.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(side_effect=Exception("err"))
        result = await _add_label(1001, "owner/repo", 1, ["bug"])
        assert result.success is False


class TestCreateLabel:
    @patch("src.tools.github.labels.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(return_value={"name": "feature", "color": "00ff00"})
        result = await _create_label(1001, "owner/repo", "feature", "00ff00")
        assert result.success is True

    @patch("src.tools.github.labels.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(side_effect=Exception("already exists"))
        result = await _create_label(1001, "owner/repo", "feature")
        assert result.success is False


class TestFactories:
    def test_make_add_label(self):
        assert make_add_label(1, "o/r").name == "add_label"

    def test_make_create_label(self):
        assert make_create_label(1, "o/r").name == "create_label"
