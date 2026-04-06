"""Tests for src.api.health — health check endpoint."""

from unittest.mock import AsyncMock, patch, MagicMock

from src.api.health import health


class TestHealth:
    @patch("src.api.health.aioredis", create=True)
    @patch("src.api.health.async_session")
    async def test_all_healthy(self, mock_session, mock_redis_mod):
        # Mock DB session
        session = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.return_value = ctx

        # Mock Redis (imported inside the function)
        with patch("redis.asyncio.from_url") as mock_from_url:
            mock_r = AsyncMock()
            mock_from_url.return_value = mock_r

            result = await health()

        assert result["status"] == "ok"
        assert "environment" in result

    @patch("src.api.health.async_session")
    async def test_db_failure_returns_degraded(self, mock_session):
        mock_session.side_effect = Exception("Connection refused")

        with patch("redis.asyncio.from_url") as mock_from_url:
            mock_r = AsyncMock()
            mock_from_url.return_value = mock_r

            result = await health()

        assert result["status"] == "degraded"
        assert "error" in result["database"]
