"""Tests for src.tools.github.repos — repo tree, file reading, collaborators."""

from unittest.mock import AsyncMock, patch
import base64

from src.tools.github.repos import (
    _get_repo_tree, _read_file, _get_collaborators,
    make_get_repo_tree, make_read_file, make_get_collaborators,
)


class TestGetRepoTree:
    @patch("src.tools.github.repos.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(return_value={
            "tree": [
                {"path": "src/main.py", "type": "blob", "size": 100},
                {"path": "src", "type": "tree"},
            ]
        })
        result = await _get_repo_tree(1001, "owner/repo")
        assert result.success is True
        assert len(result.data) == 2
        assert result.data[0]["path"] == "src/main.py"

    @patch("src.tools.github.repos.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(side_effect=Exception("not found"))
        result = await _get_repo_tree(1001, "owner/repo")
        assert result.success is False


class TestReadFile:
    @patch("src.tools.github.repos.GitHubClient")
    async def test_success_decodes_base64(self, mock_cls):
        content = base64.b64encode(b"print('hello')").decode()
        mock_cls.return_value.get = AsyncMock(return_value={
            "content": content, "size": 14,
        })
        result = await _read_file(1001, "owner/repo", "main.py")
        assert result.success is True
        assert result.data["content"] == "print('hello')"

    @patch("src.tools.github.repos.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(side_effect=Exception("404"))
        result = await _read_file(1001, "owner/repo", "missing.py")
        assert result.success is False


class TestGetCollaborators:
    @patch("src.tools.github.repos.GitHubClient")
    async def test_success(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(return_value=[
            {"login": "dev1", "permissions": {"admin": True}},
            {"login": "dev2", "permissions": {"push": True}},
        ])
        result = await _get_collaborators(1001, "owner/repo")
        assert result.success is True
        assert len(result.data) == 2

    @patch("src.tools.github.repos.GitHubClient")
    async def test_failure(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(side_effect=Exception("forbidden"))
        result = await _get_collaborators(1001, "owner/repo")
        assert result.success is False


class TestFactories:
    def test_make_get_repo_tree(self):
        assert make_get_repo_tree(1, "o/r").name == "get_repo_tree"

    def test_make_read_file(self):
        assert make_read_file(1, "o/r").name == "read_file"

    def test_make_get_collaborators(self):
        assert make_get_collaborators(1, "o/r").name == "get_collaborators"
