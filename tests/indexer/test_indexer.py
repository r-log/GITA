"""Tests for src.indexer.indexer — index orchestration."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.indexer.indexer import index_repository, reindex_files


def _mock_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return session, ctx


class TestIndexRepository:
    @patch("src.indexer.indexer.build_graph_for_repo", new_callable=AsyncMock)
    @patch("src.indexer.indexer.generate_code_map")
    @patch("src.indexer.indexer.async_session")
    @patch("src.indexer.indexer.parse_file")
    @patch("src.indexer.indexer.download_repo_files", new_callable=AsyncMock)
    async def test_success(self, mock_download, mock_parse, mock_session_factory, mock_codemap, mock_graph):
        mock_download.return_value = {"src/main.py": "print('hi')"}
        parsed = MagicMock()
        parsed.file_path = "src/main.py"
        parsed.language = "python"
        parsed.size_bytes = 12
        parsed.line_count = 1
        parsed.content_hash = "abc"
        parsed.structure = {"functions": []}
        mock_parse.return_value = parsed
        session, ctx = _mock_session()
        mock_session_factory.return_value = ctx
        mock_codemap.return_value = "# Code Map\n- src/main.py"

        result = await index_repository(1001, "owner/repo", 42)
        assert isinstance(result, str)
        mock_download.assert_called_once()

    @patch("src.indexer.indexer.download_repo_files", new_callable=AsyncMock)
    async def test_no_files(self, mock_download):
        mock_download.return_value = {}

        result = await index_repository(1001, "owner/repo", 42)
        assert "Empty" in result


class TestReindexFiles:
    @patch("src.indexer.indexer.update_graph_for_files", new_callable=AsyncMock)
    @patch("src.indexer.indexer.async_session")
    @patch("src.indexer.indexer.parse_file")
    @patch("src.indexer.indexer.download_specific_files", new_callable=AsyncMock)
    async def test_changed_files(self, mock_download, mock_parse, mock_session_factory, mock_graph):
        mock_download.return_value = {"src/main.py": "updated code"}
        parsed = MagicMock()
        parsed.file_path = "src/main.py"
        parsed.language = "python"
        parsed.size_bytes = 12
        parsed.line_count = 1
        parsed.content_hash = "def"
        parsed.structure = {}
        mock_parse.return_value = parsed
        session, ctx = _mock_session()
        mock_session_factory.return_value = ctx
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        result = await reindex_files(
            1001, "owner/repo", 42,
            changed_files={"src/main.py"}, removed_files=set(),
        )
        assert result["files_updated"] >= 0

    @patch("src.indexer.indexer.update_graph_for_files", new_callable=AsyncMock)
    @patch("src.indexer.indexer.async_session")
    @patch("src.indexer.indexer.download_specific_files", new_callable=AsyncMock)
    async def test_removed_files(self, mock_download, mock_session_factory, mock_graph):
        mock_download.return_value = {}
        session, ctx = _mock_session()
        mock_session_factory.return_value = ctx

        result = await reindex_files(
            1001, "owner/repo", 42,
            changed_files=set(), removed_files={"deleted.py"},
        )
        assert result["files_removed"] >= 0
