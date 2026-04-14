"""Per-repo cooldown — rate limits webhook-triggered jobs.

Prevents webhook storms (force-push floods, GitHub retry bursts) from
creating duplicate agent runs. One job per repo per ``window`` seconds.

Implementation is an in-memory dict of ``{repo: last_enqueue_time}``.
This is intentionally simple — no Redis, no persistence. The dict resets
when the process restarts, which is fine: the cooldown is a short-lived
safety net, not a durable record. ARQ job ID deduplication (Wall 3) and
agent_actions signature deduplication (Wall 4) handle the durable cases.

Thread safety: FastAPI runs on a single event loop, so dict access is
safe without locks. If we add multiple uvicorn workers behind a load
balancer, each worker gets its own cooldown dict — that's acceptable
because the ARQ job ID layer catches cross-worker duplicates.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Default cooldown window in seconds.
DEFAULT_COOLDOWN_SECONDS = 60

# In-memory state: repo_full_name (lowercased) → timestamp of last enqueue.
_last_enqueue: dict[str, float] = {}


def check_cooldown(
    repo_full_name: str,
    *,
    window: int = DEFAULT_COOLDOWN_SECONDS,
) -> bool:
    """Return True if the repo is still in cooldown (should be skipped).

    Returns False if the repo is NOT in cooldown (safe to enqueue).
    """
    key = repo_full_name.lower()
    now = time.monotonic()
    last = _last_enqueue.get(key)

    if last is not None and (now - last) < window:
        remaining = window - (now - last)
        logger.info(
            "cooldown_active repo=%s remaining=%.1fs",
            repo_full_name,
            remaining,
        )
        return True

    return False


def record_enqueue(repo_full_name: str) -> None:
    """Record that a job was just enqueued for this repo."""
    key = repo_full_name.lower()
    _last_enqueue[key] = time.monotonic()
    logger.debug("cooldown_recorded repo=%s", repo_full_name)


def reset() -> None:
    """Clear all cooldown state. Used by tests."""
    _last_enqueue.clear()
