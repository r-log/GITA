"""Tests for src.core.database — get_db dependency."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


class TestGetDb:
    @patch("src.core.database.async_session")
    async def test_yields_session_and_commits(self, mock_session_factory):
        session = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory.return_value = ctx

        from src.core.database import get_db
        gen = get_db()
        result = await gen.__anext__()
        assert result is session

        # Simulate end of request (no exception)
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass

        session.commit.assert_called_once()
        session.close.assert_called_once()

    @patch("src.core.database.async_session")
    async def test_rollback_on_exception(self, mock_session_factory):
        session = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory.return_value = ctx

        from src.core.database import get_db
        gen = get_db()
        await gen.__anext__()

        # Simulate exception during request
        with pytest.raises(ValueError):
            await gen.athrow(ValueError("db error"))

        session.rollback.assert_called_once()
        session.close.assert_called_once()
