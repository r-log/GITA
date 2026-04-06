"""Tests for src.indexer.downloader — file download and filtering."""

from unittest.mock import AsyncMock, patch

from src.indexer.downloader import _should_skip, download_repo_files, download_specific_files
from src.tools.base import ToolResult


class TestShouldSkip:
    def test_binary_extension(self):
        assert _should_skip("image.png", 100) is True
        assert _should_skip("data.pdf", 100) is True
        assert _should_skip("lib.dll", 100) is True

    def test_lock_file(self):
        assert _should_skip("package-lock.json", 100) is True
        assert _should_skip("yarn.lock", 100) is True

    def test_skip_directory(self):
        assert _should_skip("node_modules/pkg/index.js", 100) is True
        assert _should_skip("__pycache__/mod.pyc", 100) is True

    def test_oversized(self):
        assert _should_skip("big.py", 200_000) is True

    def test_minified(self):
        assert _should_skip("app.min.js", 100) is True
        assert _should_skip("styles.min.css", 100) is True

    def test_normal_source_file_passes(self):
        assert _should_skip("src/main.py", 5000) is False
        assert _should_skip("README.md", 2000) is False
        assert _should_skip("package.json", 1000) is False

    def test_egg_info_wildcard(self):
        assert _should_skip("mypackage.egg-info/PKG-INFO", 100) is True


class TestDownloadRepoFiles:
    @patch("src.indexer.downloader._read_file", new_callable=AsyncMock)
    @patch("src.indexer.downloader._get_repo_tree", new_callable=AsyncMock)
    async def test_success(self, mock_tree, mock_read):
        mock_tree.return_value = ToolResult(success=True, data=[
            {"path": "src/main.py", "type": "blob", "size": 100},
            {"path": "image.png", "type": "blob", "size": 500},  # Should be skipped
        ])
        mock_read.return_value = ToolResult(success=True, data={"content": "print('hi')"})

        files = await download_repo_files(1001, "owner/repo")
        assert "src/main.py" in files
        assert "image.png" not in files

    @patch("src.indexer.downloader._get_repo_tree", new_callable=AsyncMock)
    async def test_tree_failure(self, mock_tree):
        mock_tree.return_value = ToolResult(success=False, error="not found")

        files = await download_repo_files(1001, "owner/repo")
        assert files == {}


class TestDownloadSpecificFiles:
    @patch("src.indexer.downloader._read_file", new_callable=AsyncMock)
    async def test_downloads_requested_files(self, mock_read):
        mock_read.return_value = ToolResult(success=True, data={"content": "code"})

        files = await download_specific_files(1001, "owner/repo", ["src/main.py"])
        assert "src/main.py" in files

    @patch("src.indexer.downloader._read_file", new_callable=AsyncMock)
    async def test_skips_binary_files(self, mock_read):
        mock_read.return_value = ToolResult(success=True, data={"content": "data"})

        files = await download_specific_files(1001, "owner/repo", ["image.png", "src/main.py"])
        assert "image.png" not in files
        assert "src/main.py" in files
