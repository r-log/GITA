"""Tests for src.tools.github.checks — check run creation."""

from unittest.mock import AsyncMock, patch

from src.tools.github.checks import _create_check_run, make_create_check_run


class TestCreateCheckRun:
    @patch("src.tools.github.checks.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(return_value={
            "id": 100, "html_url": "https://github.com/..."
        })
        result = await _create_check_run(
            1001, "owner/repo", "PR Review", "abc123",
            conclusion="success", title="All clear", summary="No issues found",
        )
        assert result.success is True
        assert result.data["id"] == 100

    @patch("src.tools.github.checks.GitHubClient")
    async def test_failure_conclusion(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(return_value={
            "id": 101, "html_url": "https://..."
        })
        result = await _create_check_run(
            1001, "owner/repo", "Risk Scan", "abc123",
            conclusion="failure", title="Critical issue",
        )
        assert result.success is True

    @patch("src.tools.github.checks.GitHubClient")
    async def test_api_error(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(side_effect=Exception("forbidden"))
        result = await _create_check_run(1001, "owner/repo", "Test", "sha")
        assert result.success is False


class TestFactory:
    def test_make_create_check_run(self):
        tool = make_create_check_run(1, "o/r")
        assert tool.name == "create_check_run"
        assert "head_sha" in tool.parameters["properties"]
