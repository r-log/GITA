"""Tests for src.api.webhooks — webhook endpoint, bot detection, dedup."""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.api.webhooks import _is_bot_event, router

from tests.conftest import make_webhook_payload, compute_signature


# ---------------------------------------------------------------------------
# _is_bot_event — pure function, no mocks needed
# ---------------------------------------------------------------------------

class TestIsBotEvent:
    def test_bot_sender_type(self):
        payload = make_webhook_payload(sender={"login": "some-app", "type": "Bot", "id": 1})
        assert _is_bot_event(payload) is True

    def test_bot_login_suffix(self):
        payload = make_webhook_payload(sender={"login": "my-app[bot]", "type": "User", "id": 1})
        assert _is_bot_event(payload) is True

    def test_human_sender(self):
        payload = make_webhook_payload(sender={"login": "developer", "type": "User", "id": 1})
        assert _is_bot_event(payload) is False

    def test_empty_sender(self):
        payload = make_webhook_payload()
        payload["sender"] = {}
        assert _is_bot_event(payload) is False

    def test_no_sender_key(self):
        payload = {"action": "opened"}
        assert _is_bot_event(payload) is False


# ---------------------------------------------------------------------------
# Webhook endpoint tests — using direct function calls
# ---------------------------------------------------------------------------

class TestWebhookEndpoint:
    @patch("src.api.webhooks._get_arq_pool")
    @patch("src.api.webhooks._get_redis")
    @patch("src.api.webhooks.verify_webhook_signature")
    async def test_ping_returns_pong(self, mock_verify, mock_redis, mock_arq):
        """Ping events should return pong without queuing."""
        body = json.dumps({"zen": "test"}).encode()
        mock_verify.return_value = body

        request = AsyncMock()
        request.headers = {
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "delivery-001",
            "X-Hub-Signature-256": "sha256=test",
        }

        from src.api.webhooks import github_webhook
        result = await github_webhook(request)
        assert result["status"] == "pong"
        mock_arq.assert_not_called()

    @patch("src.api.webhooks._get_arq_pool")
    @patch("src.api.webhooks._get_redis")
    @patch("src.api.webhooks.verify_webhook_signature")
    async def test_valid_webhook_accepted_and_queued(self, mock_verify, mock_redis_fn, mock_arq):
        payload = make_webhook_payload(action="opened")
        body = json.dumps(payload).encode()
        mock_verify.return_value = body

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)  # set-if-not-exists succeeds
        mock_redis_fn.return_value = mock_redis

        mock_pool = AsyncMock()
        mock_arq.return_value = mock_pool

        request = AsyncMock()
        request.headers = {
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-002",
        }

        from src.api.webhooks import github_webhook
        result = await github_webhook(request)

        assert result["status"] == "accepted"
        mock_pool.enqueue_job.assert_called_once()

    @patch("src.api.webhooks._get_arq_pool")
    @patch("src.api.webhooks._get_redis")
    @patch("src.api.webhooks.verify_webhook_signature")
    async def test_bot_event_skipped(self, mock_verify, mock_redis_fn, mock_arq):
        payload = make_webhook_payload(sender={"login": "app[bot]", "type": "Bot", "id": 1})
        body = json.dumps(payload).encode()
        mock_verify.return_value = body

        request = AsyncMock()
        request.headers = {
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-003",
        }

        from src.api.webhooks import github_webhook
        result = await github_webhook(request)

        assert result["status"] == "skipped"
        assert result["reason"] == "bot_event"

    @patch("src.api.webhooks._get_arq_pool")
    @patch("src.api.webhooks._get_redis")
    @patch("src.api.webhooks.verify_webhook_signature")
    async def test_skip_actions(self, mock_verify, mock_redis_fn, mock_arq):
        for action in ("deleted", "transferred", "pinned", "unpinned"):
            payload = make_webhook_payload(action=action)
            body = json.dumps(payload).encode()
            mock_verify.return_value = body

            request = AsyncMock()
            request.headers = {
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": f"delivery-{action}",
            }

            from src.api.webhooks import github_webhook
            result = await github_webhook(request)
            assert result["status"] == "skipped"
            assert "ignored" in result["reason"]

    @patch("src.api.webhooks._get_arq_pool")
    @patch("src.api.webhooks._get_redis")
    @patch("src.api.webhooks.verify_webhook_signature")
    async def test_duplicate_delivery_skipped(self, mock_verify, mock_redis_fn, mock_arq):
        payload = make_webhook_payload(action="opened")
        body = json.dumps(payload).encode()
        mock_verify.return_value = body

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=None)  # set-if-not-exists fails (already exists)
        mock_redis_fn.return_value = mock_redis

        request = AsyncMock()
        request.headers = {
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-dup",
        }

        from src.api.webhooks import github_webhook
        result = await github_webhook(request)

        assert result["status"] == "skipped"
        assert result["reason"] == "duplicate_delivery"

    @patch("src.api.webhooks._get_arq_pool")
    @patch("src.api.webhooks._get_redis")
    @patch("src.api.webhooks.verify_webhook_signature")
    async def test_push_event_queues_context_update(self, mock_verify, mock_redis_fn, mock_arq):
        payload = make_webhook_payload(action="")
        body = json.dumps(payload).encode()
        mock_verify.return_value = body

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)
        mock_redis_fn.return_value = mock_redis

        mock_pool = AsyncMock()
        mock_arq.return_value = mock_pool

        request = AsyncMock()
        request.headers = {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-push",
        }

        from src.api.webhooks import github_webhook
        result = await github_webhook(request)

        assert result["status"] == "accepted"
        # Two enqueue calls: process_webhook + process_context_update
        assert mock_pool.enqueue_job.call_count == 2
        call_names = [c.args[0] for c in mock_pool.enqueue_job.call_args_list]
        assert "process_webhook" in call_names
        assert "process_context_update" in call_names
