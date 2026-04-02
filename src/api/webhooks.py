"""
GitHub webhook endpoint.
Receives events, verifies signature, and queues agent dispatch via ARQ.
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


async def _get_arq_pool():
    global _arq_pool
    if _arq_pool is None:
        from arq import create_pool
        from arq.connections import RedisSettings
        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


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

    log.info("webhook_received", action=action)

    # Skip ping events
    if event_type == "ping":
        return {"status": "pong"}

    # Queue for background processing — respond immediately
    pool = await _get_arq_pool()
    await pool.enqueue_job(
        "process_webhook",
        event_type, action, repo, installation_id, payload,
    )

    log.info("webhook_queued")

    return {"status": "accepted", "event": event_type, "delivery_id": delivery_id}
