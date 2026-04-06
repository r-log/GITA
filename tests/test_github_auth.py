"""Tests for src.core.github_auth — JWT generation and GitHubClient."""

import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.core.github_auth import _generate_jwt, get_installation_token, GitHubClient


class TestGenerateJWT:
    @patch("src.core.github_auth.settings")
    def test_returns_string(self, mock_settings):
        # Use a proper RSA key for JWT generation
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        mock_settings.github_app_id = 12345
        mock_settings.github_app_private_key = pem

        token = _generate_jwt()
        assert isinstance(token, str)
        assert len(token) > 0

    @patch("src.core.github_auth.settings")
    def test_jwt_has_three_parts(self, mock_settings):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        mock_settings.github_app_id = 12345
        mock_settings.github_app_private_key = pem

        token = _generate_jwt()
        parts = token.split(".")
        assert len(parts) == 3  # header.payload.signature


class TestGitHubClient:
    async def test_ensure_token_fetches_on_first_call(self):
        client = GitHubClient(1001)
        with patch("src.core.github_auth.get_installation_token", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "test-token-123"

            token = await client._ensure_token()
            assert token == "test-token-123"
            mock_get.assert_called_once_with(1001)

    async def test_ensure_token_caches(self):
        client = GitHubClient(1001)
        with patch("src.core.github_auth.get_installation_token", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "cached-token"

            await client._ensure_token()
            await client._ensure_token()
            # Should only fetch once
            mock_get.assert_called_once()

    async def test_ensure_token_refreshes_when_expired(self):
        client = GitHubClient(1001)
        with patch("src.core.github_auth.get_installation_token", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "new-token"

            # Force token to appear expired
            client._token = "old-token"
            client._token_expires_at = time.time() - 100  # Expired

            token = await client._ensure_token()
            assert token == "new-token"
            mock_get.assert_called_once()

    async def test_ensure_token_refreshes_within_300s_window(self):
        """Token is refreshed if within 300s of expiry."""
        client = GitHubClient(1001)
        with patch("src.core.github_auth.get_installation_token", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "refreshed-token"
            client._token = "soon-expiring"
            client._token_expires_at = time.time() + 100  # Within 300s window

            token = await client._ensure_token()
            assert token == "refreshed-token"

    @patch("httpx.AsyncClient")
    @patch("src.core.github_auth._generate_jwt")
    async def test_get_installation_token_success(self, mock_jwt, mock_httpx_cls):
        mock_jwt.return_value = "jwt-token"

        mock_response = MagicMock()
        mock_response.json.return_value = {"token": "install-token-123"}
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_cls.return_value = mock_http

        token = await get_installation_token(1001)
        assert token == "install-token-123"

    @patch("httpx.AsyncClient")
    @patch("src.core.github_auth._generate_jwt")
    async def test_get_installation_token_http_error(self, mock_jwt, mock_httpx_cls):
        mock_jwt.return_value = "jwt-token"

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(side_effect=Exception("403"))

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_cls.return_value = mock_http

        with pytest.raises(Exception):
            await get_installation_token(1001)

    async def test_get_post_patch_convenience_methods(self):
        client = GitHubClient(1001)
        with patch("src.core.github_auth.get_installation_token", new_callable=AsyncMock) as mock_get_token:
            mock_get_token.return_value = "token"
            with patch("httpx.AsyncClient") as mock_httpx_cls:
                mock_response = MagicMock()
                mock_response.json.return_value = {"result": "ok"}
                mock_response.raise_for_status = MagicMock()

                mock_http = AsyncMock()
                mock_http.request = AsyncMock(return_value=mock_response)
                mock_http.__aenter__ = AsyncMock(return_value=mock_http)
                mock_http.__aexit__ = AsyncMock(return_value=False)
                mock_httpx_cls.return_value = mock_http

                result = await client.get("/test")
                assert result == {"result": "ok"}

                result = await client.post("/test", json={"a": 1})
                assert result == {"result": "ok"}

                result = await client.patch("/test", json={"b": 2})
                assert result == {"result": "ok"}
