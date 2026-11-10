"""GitHub webhook receiver endpoint.

``POST /api/webhooks/github`` applies these gates before anything is
enqueued:

1. **HMAC-SHA256 signature** over the raw body with
   ``GITHUB_WEBHOOK_SECRET``. Missing or wrong → 401. Unconfigured secret
   → 500 (fail closed).
2. **Well-formed payload** — a JSON object and an ``X-GitHub-Event``
   header, else 400.
3. **Bot sender filter** — events sent by GITA's own bot account are
   dropped, so its comments, branches and PRs can't trigger more GITA
   work. With ``GITHUB_APP_SLUG`` set only that account is ignored (other
   bots' PRs, e.g. Dependabot's, are reviewed); unset, every bot is.
4. **Event allowlist** — ``dispatch_event`` only handles the events in
   ``EVENT_HANDLERS``; see that module for how job IDs are chosen.
5. **Job-ID dedupe** — ARQ refuses a job whose ID is already queued,
   running, or recently finished, which absorbs GitHub redeliveries.

There is deliberately no per-repo cooldown: it dropped legitimate work
(the ``pull_request.synchronize`` that accompanies every ``push``).
Bursts are absorbed by job identity and the review debounce instead.

Ignored events still get a 200 so GitHub records the delivery as handled.
GitHub does not retry failed deliveries automatically, so anything that
gets a 5xx here (queue down) needs a manual redelivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from gita.config import settings
from gita.web.dispatch import dispatch_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks")


@dataclass
class WebhookEvent:
    """Parsed webhook event metadata, extracted before dispatch."""

    event_type: str  # X-GitHub-Event header value (e.g. "pull_request")
    action: str | None  # payload.action (e.g. "opened"), None for push
    delivery_id: str | None  # X-GitHub-Delivery header (unique per delivery)
    repo_full_name: str | None  # payload.repository.full_name
    sender_login: str | None  # payload.sender.login
    sender_type: str | None  # payload.sender.type ("User" or "Bot")


def verify_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify GitHub's ``sha256=<hex>`` HMAC over the raw request body.

    Compares bytes, so a header with non-ASCII characters is a mismatch
    rather than a ``TypeError``.
    """
    if not signature_header.startswith("sha256="):
        return False
    expected = signature_header[len("sha256=") :].encode("utf-8", errors="replace")
    computed = (
        hmac.new(secret.encode("utf-8"), payload_body, hashlib.sha256).hexdigest().encode("ascii")
    )
    return hmac.compare_digest(computed, expected)


def _parse_event(
    payload: dict,
    event_type: str,
    delivery_id: str | None,
) -> WebhookEvent:
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


def _is_own_bot(event: WebhookEvent) -> bool:
    if event.sender_type != "Bot":
        return False
    slug = settings.github_app_slug
    return slug is None or event.sender_login == f"{slug}[bot]"


def _reply(status_code: int, **body: object) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=body)


@router.post("/github")
async def receive_webhook(
    request: Request,
    x_github_event: str | None = Header(None),
    x_hub_signature_256: str | None = Header(None),
    x_github_delivery: str | None = Header(None),
) -> JSONResponse:
    """Validate a GitHub webhook delivery and enqueue the resulting job."""
    body = await request.body()

    # --- Gate 1: signature ---
    secret = settings.github_webhook_secret
    if not secret:
        logger.error("GITHUB_WEBHOOK_SECRET not configured — rejecting all webhooks")
        return _reply(500, error="webhook secret not configured")
    if not x_hub_signature_256:
        logger.warning("webhook_rejected reason=missing_signature")
        return _reply(401, error="missing X-Hub-Signature-256 header")
    if not verify_signature(body, x_hub_signature_256, secret):
        logger.warning("webhook_rejected reason=invalid_signature")
        return _reply(401, error="invalid signature")

    # --- Gate 2: well-formed payload ---
    if not x_github_event:
        return _reply(400, error="missing X-GitHub-Event header")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _reply(400, error="body is not valid JSON")
    if not isinstance(payload, dict):
        return _reply(400, error="body must be a JSON object")

    if x_github_event == "ping":
        logger.info("webhook_ping zen=%s", payload.get("zen", ""))
        return _reply(200, status="pong")

    event = _parse_event(payload, x_github_event, x_github_delivery)

    # --- Gate 3: bot senders (loop prevention) ---
    if _is_own_bot(event):
        logger.info(
            "webhook_ignored_bot sender=%s event=%s action=%s repo=%s",
            event.sender_login,
            event.event_type,
            event.action,
            event.repo_full_name,
        )
        return _reply(200, status="ignored", reason="bot sender")

    # --- Gate 4: event allowlist ---
    job = await dispatch_event(event.event_type, event.action, payload)
    if job is None:
        logger.info(
            "webhook_no_handler event=%s action=%s repo=%s delivery=%s",
            event.event_type,
            event.action,
            event.repo_full_name,
            event.delivery_id,
        )
        return _reply(
            200,
            status="ignored",
            reason="no handler",
            event=event.event_type,
            action=event.action,
        )

    # --- Gate 5: enqueue with job-ID dedupe ---
    pool = getattr(request.app.state, "arq_pool", None)
    if pool is None:
        logger.error("arq_pool_unavailable — cannot enqueue job %s", job.job_id)
        return _reply(503, error="job queue unavailable")

    enqueue_kwargs = dict(job.kwargs)
    if job.defer_seconds:
        enqueue_kwargs["_defer_by"] = job.defer_seconds
    arq_job = await pool.enqueue_job(job.function_name, _job_id=job.job_id, **enqueue_kwargs)

    if arq_job is None:
        logger.info(
            "webhook_job_deduped job_id=%s function=%s delivery=%s",
            job.job_id,
            job.function_name,
            event.delivery_id,
        )
        return _reply(200, status="ignored", reason="job already queued", job_id=job.job_id)

    logger.info(
        "webhook_dispatched job_id=%s function=%s repo=%s delivery=%s",
        job.job_id,
        job.function_name,
        job.repo_full_name,
        event.delivery_id,
    )
    return _reply(200, status="dispatched", job_id=job.job_id, function=job.function_name)
