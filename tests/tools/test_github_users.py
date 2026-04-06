"""Tests for src.tools.github.users — user tagging."""

from unittest.mock import AsyncMock, patch

from src.tools.github.users import _tag_user, make_tag_user


class TestTagUser:
    @patch("src.tools.github.users.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(return_value={
            "id": 200, "html_url": "https://github.com/..."
        })
        result = await _tag_user(1001, "owner/repo", 5, ["dev1", "dev2"], "Please review")
        assert result.success is True
        # Verify the body contains mentions
        call_kwargs = mock_cls.return_value.post.call_args[1]["json"]
        assert "@dev1" in call_kwargs["body"]
        assert "@dev2" in call_kwargs["body"]
        assert "Please review" in call_kwargs["body"]

    @patch("src.tools.github.users.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.post = AsyncMock(side_effect=Exception("forbidden"))
        result = await _tag_user(1001, "owner/repo", 5, ["dev"], "msg")
        assert result.success is False


class TestFactory:
    def test_make_tag_user(self):
        tool = make_tag_user(1, "o/r")
        assert tool.name == "tag_user"
        assert "usernames" in tool.parameters["properties"]
