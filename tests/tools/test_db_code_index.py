"""Tests for src.tools.db.code_index — code index queries and issue records."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.tools.db.code_index import (
    _query_code_index, _get_code_map, _save_issue_record,
    make_query_code_index, make_get_code_map, make_save_issue_record,
)


def _mock_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    session.refresh = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return session, ctx


class TestQueryCodeIndex:
    @patch("src.tools.db.code_index.async_session")
    async def test_by_path(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )

        result = await _query_code_index(42, file_path="src/main.py")
        assert result.success is True

    @patch("src.tools.db.code_index.async_session")
    async def test_by_language(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )

        result = await _query_code_index(42, language="python")
        assert result.success is True

    @patch("src.tools.db.code_index.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _query_code_index(42)
        assert result.success is False


class TestGetCodeMap:
    @patch("src.tools.db.code_index.async_session")
    async def test_success(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        )

        result = await _get_code_map(42)
        assert result.success is True

    @patch("src.tools.db.code_index.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _get_code_map(42)
        assert result.success is False


class TestSaveIssueRecord:
    @patch("src.tools.db.code_index.async_session")
    async def test_success(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        result = await _save_issue_record(42, 5, "Bug fix", "open", ["bug"], False, None)
        assert result.success is True

    @patch("src.tools.db.code_index.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _save_issue_record(42, 5, "Bug", "open", [], False, None)
        assert result.success is False


class TestFactories:
    def test_make_query_code_index(self):
        assert make_query_code_index(42).name == "query_code_index"

    def test_make_get_code_map(self):
        assert make_get_code_map(42).name == "get_code_map"

    def test_make_save_issue_record(self):
        assert make_save_issue_record(42).name == "save_issue_record"
