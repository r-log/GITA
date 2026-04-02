"""
GitHub webhook endpoint.
Receives events, verifies signature, and queues agent dispatch.
"""

import json
import structlog
from fastapi import APIRouter, Request

from src.core.security import verify_webhook_signature

log = structlog.get_logger()

router = APIRouter()


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
        event=event_type,
        repo=repo,
    )

    log.info("webhook_received", action=action)

    # Skip ping events
    if event_type == "ping":
        return {"status": "pong"}

    # Dispatch inline (in production, queue via ARQ)
    from src.workers.tasks import dispatch_event
    await dispatch_event(event_type, action, repo, installation_id, payload)

    return {"status": "accepted", "event": event_type, "delivery_id": delivery_id}
