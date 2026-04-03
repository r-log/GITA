"""
GitHub webhook endpoint.
Receives events, verifies signature, and queues agent dispatch via ARQ.

Includes three layers of spam prevention:
1. Bot self-detection — skip events triggered by our own app
2. Delivery dedup — don't process the same webhook twice
3. Comment locking — handled at the comment posting level
"""

import json
import structlog
from fastapi import APIRouter, Request

from src.core.security import verify_webhook_signature
from src.core.config import settings

log = structlog.get_logger()

router = APIRouter()

# ARQ redis pool — initialized on first use
_arq_pool = None
# Redis client for dedup — initialized on first use
_redis = None


async def _get_arq_pool():
    global _arq_pool
    if _arq_pool is None:
        from arq import create_pool
        from arq.connections import RedisSettings
        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


async def _get_redis():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(settings.redis_url)
    return _redis


def _is_bot_event(payload: dict) -> bool:
    """Check if this event was triggered by our own bot (GitHub App)."""
    sender = payload.get("sender", {})

    # GitHub Apps have sender.type == "Bot"
    if sender.get("type") == "Bot":
        return True

    # Also check the login — GitHub App bots are named like "app-name[bot]"
    login = sender.get("login", "")
    if login.endswith("[bot]"):
        return True

    return False


@router.post("/api/webhooks/github")
async def github_webhook(request: Request):
    """Receive GitHub webhook events, verify signature, and queue for processing."""
    body = await verify_webhook_signature(request)
    event_type = request.headers.get("X-GitHub-Event", "unknown")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    payload = json.loads(body)

    action = payload.get("action", "")
    repo = payload.get("repository", {}).get("full_name", "unknown")
    installation_id = payload.get("installation", {}).get("id", 0)

    # Bind correlation ID to all log messages in this request
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        delivery_id=delivery_id,
        webhook_event=event_type,
        repo=repo,
    )

    # Skip ping events
    if event_type == "ping":
        return {"status": "pong"}

    # LAYER 1: Skip events triggered by our own bot
    if _is_bot_event(payload):
        log.info("webhook_skipped_bot", action=action)
        return {"status": "skipped", "reason": "bot_event"}

    # LAYER 2: Delivery deduplication — skip if we already processed this delivery
    r = await _get_redis()
    dedup_key = f"delivery:{delivery_id}"
    already_processed = await r.set(dedup_key, "1", ex=300, nx=True)  # 5 min TTL, set-if-not-exists
    if not already_processed:
        log.info("webhook_skipped_dedup", action=action)
        return {"status": "skipped", "reason": "duplicate_delivery"}

    log.info("webhook_received", action=action)

    # Queue for background processing — respond immediately
    pool = await _get_arq_pool()
    await pool.enqueue_job(
        "process_webhook",
        event_type, action, repo, installation_id, payload,
    )

    log.info("webhook_queued")

    return {"status": "accepted", "event": event_type, "delivery_id": delivery_id}
