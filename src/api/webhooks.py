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
from src.core.repo_manager import upsert_repository
from src.tools.db.entity_sync import persist_event, persist_issue_from_payload, persist_pr_from_payload, persist_comment

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

    # Skip event types GITA doesn't handle (no agents in routing table)
    skip_events = {"check_suite", "check_run", "workflow_run", "workflow_job", "status", "deployment", "deployment_status", "release", "create", "delete", "fork", "watch", "star", "member"}
    if event_type in skip_events:
        return {"status": "skipped", "reason": f"event_{event_type}_not_handled"}

    # Skip events that don't need agent processing
    skip_actions = {"deleted", "transferred", "pinned", "unpinned"}
    if action in skip_actions:
        log.info("webhook_skipped_action", action=action)
        return {"status": "skipped", "reason": f"action_{action}_ignored"}

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

    # Resolve repo_id and persist event + entities for RAG
    repo_github_id = payload.get("repository", {}).get("id", 0)
    repo_id = 0
    if repo_github_id:
        try:
            repo_id = await upsert_repository(repo_github_id, repo, installation_id)

            # Classify target for event indexing
            target_type = None
            target_number = None
            if event_type == "issues":
                target_type = "issue"
                target_number = payload.get("issue", {}).get("number")
            elif event_type == "pull_request":
                target_type = "pr"
                target_number = payload.get("pull_request", {}).get("number")
            elif event_type == "push":
                target_type = "push"
            elif event_type == "issue_comment":
                target_type = "issue"
                target_number = payload.get("issue", {}).get("number")

            sender_login = payload.get("sender", {}).get("login")

            # Persist raw event
            await persist_event(
                repo_id, delivery_id, event_type, action,
                sender_login, target_type, target_number, payload,
            )

            # Persist enriched entities from the payload
            if event_type == "issues" and "issue" in payload:
                await persist_issue_from_payload(repo_id, payload["issue"])
            elif event_type == "pull_request" and "pull_request" in payload:
                await persist_pr_from_payload(repo_id, payload["pull_request"])
            elif event_type == "issue_comment" and "comment" in payload:
                issue_number = payload.get("issue", {}).get("number", 0)
                await persist_comment(repo_id, payload["comment"], "issue", issue_number)
        except Exception as e:
            log.warning("webhook_persist_failed", error=str(e))

    # Queue for background processing — respond immediately
    pool = await _get_arq_pool()
    await pool.enqueue_job(
        "process_webhook",
        event_type, action, repo, installation_id, payload,
    )

    log.info("webhook_queued")

    # Also enqueue context update for push events (runs in parallel with agent dispatch)
    if event_type == "push":
        await pool.enqueue_job(
            "process_context_update",
            repo, installation_id, payload,
        )
        log.info("context_update_queued")

    return {"status": "accepted", "event": event_type, "delivery_id": delivery_id}
