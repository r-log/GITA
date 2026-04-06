"""Tests for src.tools.db.onboarding — onboarding run and file mapping persistence."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.tools.db.onboarding import (
    _save_onboarding_run, _save_file_mapping,
    make_save_onboarding_run, make_save_file_mapping,
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


class TestSaveOnboardingRun:
    @patch("src.tools.db.onboarding.async_session")
    async def test_success(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx

        result = await _save_onboarding_run(
            repo_id=42, status="success",
            repo_snapshot={"files": 10}, suggested_plan={"milestones": []},
            existing_state={}, actions_taken=[],
            milestones_created=2, issues_created=5,
        )
        assert result.success is True
        session.add.assert_called_once()

    @patch("src.tools.db.onboarding.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _save_onboarding_run(42, "failed", {}, {}, {}, [])
        assert result.success is False


class TestSaveFileMapping:
    @patch("src.tools.db.onboarding.async_session")
    async def test_success(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx

        result = await _save_file_mapping(42, "src/main.py", milestone_id=1, issue_id=5, confidence=0.9)
        assert result.success is True

    @patch("src.tools.db.onboarding.async_session")
    async def test_no_milestone_or_issue(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx

        result = await _save_file_mapping(42, "src/main.py")
        assert result.success is True

    @patch("src.tools.db.onboarding.async_session")
    async def test_error(self, mock_factory):
        mock_factory.side_effect = Exception("db error")
        result = await _save_file_mapping(42, "src/main.py")
        assert result.success is False


class TestFactories:
    def test_make_save_onboarding_run(self):
        assert make_save_onboarding_run(42).name == "save_onboarding_run"

    def test_make_save_file_mapping(self):
        assert make_save_file_mapping(42).name == "save_file_mapping"
