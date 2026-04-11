"""
Outcome worker tasks — the measurement layer.

Two entry points:

- `run_outcome_checks(ctx)`: cron entry that runs every 5 minutes. Marks stale
  outcomes (>7 days old, still pending), finds due outcomes (status='pending'
  AND scheduled_for <= now()), and enqueues a `check_outcome` job per row on
  the `outcomes` named queue. No retries, no escalation.

- `check_outcome(ctx, outcome_id)`: loads one row, looks up the registered
  checker for its outcome_type, calls it, and writes status/observed/notes/
  checked_at back to the row. Never retries on checker errors — just marks
  the row as failed with the error in notes.

All work happens in the world model. Checkers never call the GitHub API.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from sqlalchemy import select, update

from src.core.database import async_session
from src.models.outcome import OutcomeModel, OutcomeStatus
from src.workers.outcome_registry import get_checker

# Import the checkers module so register_checker() calls populate OUTCOME_CHECKERS.
# Side-effect import — required for checker lookup to succeed at runtime.
import src.workers.outcome_checkers  # noqa: F401

log = structlog.get_logger()


STALE_AFTER = timedelta(days=7)
DUE_BATCH_LIMIT = 50


async def check_outcome(ctx, outcome_id: int) -> dict:
    """
    Check a single outcome row. Loads it, calls the registered checker,
    writes the verdict back. Swallows all exceptions — a bad checker never
    brings the worker down.
    """
    async with async_session() as session:
        row = await session.get(OutcomeModel, outcome_id)
        if not row:
            log.warning("check_outcome_missing", outcome_id=outcome_id)
            return {"status": "missing"}

        if row.status != OutcomeStatus.PENDING.value:
            log.info(
                "check_outcome_skipped",
                outcome_id=outcome_id,
                current_status=row.status,
            )
            return {"status": "skipped", "current_status": row.status}

        checker = get_checker(row.outcome_type)
        if not checker:
            log.warning(
                "check_outcome_no_checker",
                outcome_id=outcome_id,
                outcome_type=row.outcome_type,
            )
            await session.execute(
                update(OutcomeModel)
                .where(OutcomeModel.id == outcome_id)
                .values(
                    status=OutcomeStatus.FAILED.value,
                    notes=f"No checker registered for {row.outcome_type}",
                    checked_at=datetime.utcnow(),
                )
            )
            await session.commit()
            return {"status": "failed", "reason": "no_checker"}

        try:
            result = await checker(
                row.repo_id,
                row.target_number,
                row.predicted or {},
                session,
            )
        except Exception as e:
            log.exception("checker_raised", outcome_id=outcome_id, outcome_type=row.outcome_type)
            await session.execute(
                update(OutcomeModel)
                .where(OutcomeModel.id == outcome_id)
                .values(
                    status=OutcomeStatus.FAILED.value,
                    notes=f"Checker raised: {e}",
                    checked_at=datetime.utcnow(),
                )
            )
            await session.commit()
            return {"status": "failed", "reason": "checker_raised"}

        await session.execute(
            update(OutcomeModel)
            .where(OutcomeModel.id == outcome_id)
            .values(
                status=result.status,
                observed=result.observed or {},
                notes=result.notes,
                checked_at=datetime.utcnow(),
            )
        )
        await session.commit()

        log.info(
            "outcome_checked",
            outcome_id=outcome_id,
            outcome_type=row.outcome_type,
            verdict=result.status,
        )
        return {"status": result.status, "outcome_id": outcome_id}


async def run_outcome_checks(ctx) -> dict:
    """
    Cron entry. Runs every 5 minutes.

    1. Mark stale outcomes (pending > 7 days) as 'stale'.
    2. Find due outcomes (pending AND scheduled_for <= now) and enqueue
       a `check_outcome` job per row on the `outcomes` named queue.
    """
    now = datetime.utcnow()
    stale_cutoff = now - STALE_AFTER

    async with async_session() as session:
        # 1. Flip stale rows
        stale_stmt = (
            update(OutcomeModel)
            .where(
                OutcomeModel.status == OutcomeStatus.PENDING.value,
                OutcomeModel.created_at < stale_cutoff,
            )
            .values(
                status=OutcomeStatus.STALE.value,
                checked_at=now,
                notes="Marked stale — pending > 7 days",
            )
        )
        stale_result = await session.execute(stale_stmt)
        stale_count = stale_result.rowcount or 0

        # 2. Find due outcomes
        due_stmt = (
            select(OutcomeModel.id)
            .where(
                OutcomeModel.status == OutcomeStatus.PENDING.value,
                OutcomeModel.scheduled_for <= now,
            )
            .order_by(OutcomeModel.scheduled_for)
            .limit(DUE_BATCH_LIMIT)
        )
        due_ids = [row[0] for row in (await session.execute(due_stmt)).all()]

        await session.commit()

    if stale_count:
        log.info("outcomes_marked_stale", count=stale_count)

    # 3. Enqueue check jobs
    enqueued = 0
    if due_ids:
        redis = ctx.get("redis") if isinstance(ctx, dict) else getattr(ctx, "get", lambda k: None)("redis")
        if redis is None:
            log.warning("run_outcome_checks_no_redis", due_count=len(due_ids))
        else:
            for outcome_id in due_ids:
                try:
                    await redis.enqueue_job(
                        "check_outcome",
                        outcome_id,
                        _queue_name="outcomes",
                    )
                    enqueued += 1
                except Exception as e:
                    log.warning(
                        "check_outcome_enqueue_failed",
                        outcome_id=outcome_id,
                        error=str(e),
                    )

        log.info("outcomes_enqueued", due=len(due_ids), enqueued=enqueued)

    return {
        "stale_marked": stale_count,
        "due_found": len(due_ids),
        "enqueued": enqueued,
    }
