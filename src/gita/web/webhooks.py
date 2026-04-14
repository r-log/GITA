"""GitHub webhook receiver endpoint.

Handles ``POST /api/webhooks/github`` with three layers of validation
before any work is dispatched:

1. **HMAC-SHA256 signature verification** — rejects payloads that weren't
   signed by the configured ``GITHUB_WEBHOOK_SECRET``. Returns 401 on
   mismatch or missing header.

2. **Bot sender filter (Wall 2)** — drops events where
   ``payload.sender.type == "Bot"``. When GITA posts a comment, GitHub
   fires a webhook for that comment. Without this filter, the webhook
   could trigger another agent run, creating an infinite loop. Returns
   200 OK (GitHub doesn't need to retry) but enqueues nothing.

3. **Event dispatch (Wall 1)** — routes the event through
   ``dispatch_event`` which only handles allowlisted event types.
   Unknown events get a 200 OK with no dispatch.

4. **Per-repo cooldown** — rate limits to one job per repo per 60s.
   Prevents webhook storms from creating duplicate agent runs.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Header, Request, Response

from gita.config import settings
from gita.web.cooldown import check_cooldown, record_enqueue
from gita.web.dispatch import dispatch_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks")


@dataclass
class WebhookEvent:
    """Parsed webhook event metadata, extracted before dispatch."""

    event_type: str          # X-GitHub-Event header value (e.g. "pull_request")
    action: str | None       # payload.action (e.g. "opened"), None for push
    delivery_id: str | None  # X-GitHub-Delivery header (unique per delivery)
    repo_full_name: str | None   # payload.repository.full_name
    sender_login: str | None     # payload.sender.login
    sender_type: str | None      # payload.sender.type ("User" or "Bot")


def verify_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify the HMAC-SHA256 signature on a GitHub webhook payload.

    GitHub sends the signature as ``sha256=<hex>``. We compute our own
    HMAC over the raw request body using the shared secret and compare
    in constant time.
    """
    if not signature_header.startswith("sha256="):
        return False

    expected_signature = signature_header[7:]  # strip "sha256=" prefix
    computed = hmac.new(
        secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, expected_signature)


def _parse_event(
    payload: dict,
    event_type: str,
    delivery_id: str | None,
) -> WebhookEvent:
    """Extract structured metadata from the raw webhook payload."""
    sender = payload.get("sender") or {}
    repo = payload.get("repository") or {}

    return WebhookEvent(
        event_type=event_type,
        action=payload.get("action"),
        delivery_id=delivery_id,
        repo_full_name=repo.get("full_name"),
        sender_login=sender.get("login"),
        sender_type=sender.get("type"),
    )


@router.post("/github")
async def receive_webhook(
    request: Request,
    x_github_event: str | None = Header(None),
    x_hub_signature_256: str | None = Header(None),
    x_github_delivery: str | None = Header(None),
) -> Response:
    """Receive and validate a GitHub webhook delivery.

    Returns 200 for all valid payloads (even ignored ones) so GitHub
    doesn't retry. Returns 401 for signature failures. Returns 400
    for missing event type header.
    """
    # --- Read raw body for HMAC verification ---
    body = await request.body()

    # --- Gate 1: HMAC signature verification ---
    secret = settings.github_webhook_secret
    if not secret:
        logger.error("GITHUB_WEBHOOK_SECRET not configured — rejecting all webhooks")
        return Response(
            content='{"error": "webhook secret not configured"}',
            status_code=500,
            media_type="application/json",
        )

    if not x_hub_signature_256:
        logger.warning("webhook_rejected reason=missing_signature")
        return Response(
            content='{"error": "missing X-Hub-Signature-256 header"}',
            status_code=401,
            media_type="application/json",
        )

    if not verify_signature(body, x_hub_signature_256, secret):
        logger.warning("webhook_rejected reason=invalid_signature")
        return Response(
            content='{"error": "invalid signature"}',
            status_code=401,
            media_type="application/json",
        )

    # --- Parse JSON payload ---
    payload = await request.json()

    # --- Gate 2: Event type header required ---
    if not x_github_event:
        return Response(
            content='{"error": "missing X-GitHub-Event header"}',
            status_code=400,
            media_type="application/json",
        )

    # --- Handle GitHub ping event (sent on webhook setup) ---
    if x_github_event == "ping":
        zen = payload.get("zen", "")
        logger.info("webhook_ping zen=%s", zen)
        return Response(
            content='{"status": "pong"}',
            status_code=200,
            media_type="application/json",
        )

    # --- Parse event metadata ---
    event = _parse_event(payload, x_github_event, x_github_delivery)

    # --- Gate 3: Bot sender filter (Wall 2 — loop prevention) ---
    if event.sender_type == "Bot":
        logger.info(
            "webhook_ignored_bot sender=%s event=%s action=%s repo=%s",
            event.sender_login,
            event.event_type,
            event.action,
            event.repo_full_name,
        )
        return Response(
            content='{"status": "ignored", "reason": "bot sender"}',
            status_code=200,
            media_type="application/json",
        )

    # --- Gate 4: Event dispatch (Wall 1 — event type allowlist) ---
    job = await dispatch_event(event.event_type, event.action, payload)

    if job is None:
        logger.info(
            "webhook_no_handler event=%s action=%s repo=%s",
            event.event_type,
            event.action,
            event.repo_full_name,
        )
        return Response(
            content=(
                '{"status": "ignored", "reason": "no handler", '
                f'"event": "{event.event_type}", '
                f'"action": "{event.action}"}}'
            ),
            status_code=200,
            media_type="application/json",
        )

    # --- Gate 5: Per-repo cooldown ---
    if check_cooldown(job.repo_full_name):
        logger.info(
            "webhook_cooldown repo=%s job=%s",
            job.repo_full_name,
            job.job_id,
        )
        return Response(
            content=(
                '{"status": "ignored", "reason": "cooldown", '
                f'"repo": "{job.repo_full_name}"}}'
            ),
            status_code=200,
            media_type="application/json",
        )

    # --- Enqueue the job via ARQ Redis pool ---
    pool = getattr(request.app.state, "arq_pool", None)
    if pool is None:
        logger.error("arq_pool_unavailable — cannot enqueue job %s", job.job_id)
        return Response(
            content='{"error": "job queue unavailable"}',
            status_code=503,
            media_type="application/json",
        )

    arq_job = await pool.enqueue_job(
        job.function_name,
        _job_id=job.job_id,
        **job.kwargs,
    )

    if arq_job is None:
        # ARQ returns None when a job with the same _job_id is already
        # queued or running (Wall 3 — job ID deduplication).
        logger.info(
            "webhook_job_deduped job_id=%s function=%s",
            job.job_id,
            job.function_name,
        )
        return Response(
            content=(
                '{"status": "ignored", "reason": "job already queued", '
                f'"job_id": "{job.job_id}"}}'
            ),
            status_code=200,
            media_type="application/json",
        )

    record_enqueue(job.repo_full_name)

    logger.info(
        "webhook_dispatched job_id=%s function=%s repo=%s delivery=%s",
        job.job_id,
        job.function_name,
        job.repo_full_name,
        event.delivery_id,
    )

    return Response(
        content=(
            '{"status": "dispatched", '
            f'"job_id": "{job.job_id}", '
            f'"function": "{job.function_name}"}}'
        ),
        status_code=200,
        media_type="application/json",
    )
