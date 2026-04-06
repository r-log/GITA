"""Tests for src.tools.db.analysis — evaluation and analysis CRUD."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.tools.db.analysis import (
    _save_evaluation, _get_previous_evaluation, _save_analysis, _get_analysis_history,
    make_save_evaluation, make_get_previous_evaluation, make_save_analysis, make_get_analysis_history,
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


class TestSaveEvaluation:
    @patch("src.tools.db.analysis._resolve_issue_db_id", new_callable=AsyncMock)
    @patch("src.tools.db.analysis.async_session")
    async def test_success(self, mock_factory, mock_resolve):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        mock_resolve.return_value = 100  # resolved issue DB id

        result = await _save_evaluation(42, 5, False, {"score": 8})
        assert result.success is True

    @patch("src.tools.db.analysis.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _save_evaluation(42, 5, False, {})
        assert result.success is False


class TestGetPreviousEvaluation:
    @patch("src.tools.db.analysis.async_session")
    async def test_found(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        mock_eval = MagicMock()
        mock_eval.id = 1
        mock_eval.overall_score = 8.0
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=mock_eval))

        result = await _get_previous_evaluation(42, 5)
        assert result.success is True

    @patch("src.tools.db.analysis.async_session")
    async def test_not_found(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        result = await _get_previous_evaluation(42, 5)
        assert result.success is True
        assert result.data is None


class TestSaveAnalysis:
    @patch("src.tools.db.analysis.async_session")
    async def test_success(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx

        result = await _save_analysis(42, "issue", 5, "smart_eval", {"score": 8}, 8.0, "low")
        assert result.success is True

    @patch("src.tools.db.analysis.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db")
        result = await _save_analysis(42, "issue", 5, "smart_eval", {}, None, None)
        assert result.success is False


class TestGetAnalysisHistory:
    @patch("src.tools.db.analysis.async_session")
    async def test_returns_results(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute.return_value = mock_result

        result = await _get_analysis_history(42, "issue", 5)
        assert result.success is True


class TestFactories:
    def test_make_save_evaluation(self):
        assert make_save_evaluation(42).name == "save_evaluation"

    def test_make_get_previous_evaluation(self):
        assert make_get_previous_evaluation(42).name == "get_previous_evaluation"

    def test_make_save_analysis(self):
        assert make_save_analysis(42).name == "save_analysis"

    def test_make_get_analysis_history(self):
        assert make_get_analysis_history(42).name == "get_analysis_history"
