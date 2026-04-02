"""
GitHub webhook signature verification.
Ensures webhooks actually come from GitHub, not an attacker.
"""

import hashlib
import hmac

from fastapi import Request, HTTPException

from src.core.config import settings


async def verify_webhook_signature(request: Request) -> bytes:
    """
    Verify the X-Hub-Signature-256 header from GitHub.
    Returns the raw body if valid, raises 403 if not.
    """
    signature_header = request.headers.get("X-Hub-Signature-256")
    if not signature_header:
        raise HTTPException(status_code=403, detail="Missing signature header")

    body = await request.body()

    # Compute expected signature
    expected_signature = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(expected_signature, signature_header):
        raise HTTPException(status_code=403, detail="Invalid signature")

    return body
