"""Tests for src.core.security — webhook signature verification."""

import hashlib
import hmac

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock

from src.core.security import verify_webhook_signature


def _make_request(body: bytes, signature: str | None = None) -> AsyncMock:
    """Create a mock FastAPI Request with configurable headers and body."""
    request = AsyncMock()
    request.body = AsyncMock(return_value=body)
    headers = {}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    request.headers = headers
    return request


def _compute_sig(body: bytes, secret: str = "test-webhook-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestVerifyWebhookSignature:
    async def test_valid_signature_returns_body(self):
        body = b'{"action":"opened"}'
        sig = _compute_sig(body)
        request = _make_request(body, sig)

        result = await verify_webhook_signature(request)
        assert result == body

    async def test_missing_signature_header_raises_403(self):
        request = _make_request(b'{"test": 1}', signature=None)

        with pytest.raises(HTTPException) as exc_info:
            await verify_webhook_signature(request)
        assert exc_info.value.status_code == 403
        assert "Missing signature" in exc_info.value.detail

    async def test_invalid_signature_raises_403(self):
        body = b'{"action":"opened"}'
        request = _make_request(body, "sha256=invalid_hex_string")

        with pytest.raises(HTTPException) as exc_info:
            await verify_webhook_signature(request)
        assert exc_info.value.status_code == 403
        assert "Invalid signature" in exc_info.value.detail

    async def test_wrong_secret_raises_403(self):
        body = b'{"action":"opened"}'
        wrong_sig = _compute_sig(body, secret="wrong-secret")
        request = _make_request(body, wrong_sig)

        with pytest.raises(HTTPException) as exc_info:
            await verify_webhook_signature(request)
        assert exc_info.value.status_code == 403

    async def test_modified_body_raises_403(self):
        original_body = b'{"action":"opened"}'
        sig = _compute_sig(original_body)
        modified_body = b'{"action":"closed"}'
        request = _make_request(modified_body, sig)

        with pytest.raises(HTTPException) as exc_info:
            await verify_webhook_signature(request)
        assert exc_info.value.status_code == 403

    async def test_empty_body_with_valid_signature(self):
        body = b""
        sig = _compute_sig(body)
        request = _make_request(body, sig)

        result = await verify_webhook_signature(request)
        assert result == b""

    async def test_uses_constant_time_comparison(self):
        """Verify hmac.compare_digest is used (not ==) by checking the source."""
        import inspect
        source = inspect.getsource(verify_webhook_signature)
        assert "compare_digest" in source
