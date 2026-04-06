"""Tests for src.core.repo_manager — repository upsert."""

from unittest.mock import AsyncMock, MagicMock, patch


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


class TestUpsertRepository:
    @patch("src.core.repo_manager.async_session")
    async def test_creates_new_repo(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))

        from src.core.repo_manager import upsert_repository
        result = await upsert_repository(999, "owner/repo", 1001)
        session.add.assert_called_once()
        session.commit.assert_called()

    @patch("src.core.repo_manager.async_session")
    async def test_returns_existing_repo_id(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        existing = MagicMock()
        existing.id = 42
        existing.installation_id = 1001
        existing.full_name = "owner/repo"
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=existing))

        from src.core.repo_manager import upsert_repository
        result = await upsert_repository(999, "owner/repo", 1001)
        assert result == 42
        session.add.assert_not_called()

    @patch("src.core.repo_manager.async_session")
    async def test_updates_installation_id_if_changed(self, mock_factory):
        session, ctx = _mock_session()
        mock_factory.return_value = ctx
        existing = MagicMock()
        existing.id = 42
        existing.installation_id = 999  # Different from what we pass
        existing.full_name = "owner/repo"
        session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=existing))

        from src.core.repo_manager import upsert_repository
        result = await upsert_repository(999, "owner/repo", 2000)
        assert result == 42
        session.commit.assert_called()
