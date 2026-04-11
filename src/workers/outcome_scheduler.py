"""
Outcome scheduler — called by the Supervisor after each successful agent run.

Builds outcome rows from either the agent's own predictions
(`result.data["outcome_predictions"]`) or from the default outcomes
registered for that agent in `outcome_registry.py`.

Enforces dedup (one row per agent_run + outcome_type) and caps at 3
outcomes per run. All failures are swallowed — outcome scheduling must
never crash an agent run.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from src.core.database import async_session
from src.models.outcome import OutcomeModel, OutcomeStatus
from src.workers.outcome_registry import (
    AGENT_DEFAULT_OUTCOMES,
    OUTCOME_DEFAULT_DELAYS,
)

if TYPE_CHECKING:
    from src.agents.base import AgentContext, AgentResult

log = structlog.get_logger()


MAX_OUTCOMES_PER_RUN = 3


def _extract_target(context: "AgentContext") -> tuple[str, int | None]:
    """Infer (target_type, target_number) from the event payload."""
    payload = context.event_payload or {}
    event_type = context.event_type or ""

    if "pull_request" in event_type:
        pr = payload.get("pull_request", {})
        return ("pr", pr.get("number"))

    if "issues" in event_type or event_type.startswith("issue_comment"):
        issue = payload.get("issue", {})
        return ("issue", issue.get("number"))

    if "milestone" in event_type:
        ms = payload.get("milestone", {})
        return ("milestone", ms.get("number"))

    # Fallback: look for any known key
    for key, t in (("pull_request", "pr"), ("issue", "issue"), ("milestone", "milestone")):
        if key in payload and "number" in payload[key]:
            return (t, payload[key]["number"])

    return ("unknown", None)


def _build_prediction_entry(
    outcome_type: str,
    target_type: str,
    target_number: int | None,
    predicted: dict | None = None,
    delay_override: timedelta | None = None,
) -> dict:
    """Shape a single prediction entry. Used for both agent-emitted and default predictions."""
    return {
        "outcome_type": outcome_type,
        "target_type": target_type,
        "target_number": target_number,
        "predicted": predicted or {},
        "delay_override": delay_override,
    }


def _default_predictions_for(
    agent_name: str,
    context: "AgentContext",
    result: "AgentResult",
) -> list[dict]:
    """
    When an agent doesn't set result.data['outcome_predictions'], fall back to
    the registered defaults for that agent and build minimal prediction payloads.
    """
    outcome_types = AGENT_DEFAULT_OUTCOMES.get(agent_name, [])
    if not outcome_types:
        return []

    target_type, target_number = _extract_target(context)
    if target_number is None:
        # Can't build a default outcome without a target
        return []

    # Minimal predicted payload — agents that want richer data should
    # set result.data["outcome_predictions"] themselves
    base_predicted = {
        "agent": agent_name,
        "event_type": context.event_type,
        "scheduled_at": datetime.utcnow().isoformat(),
    }

    return [
        _build_prediction_entry(
            outcome_type=ot,
            target_type=target_type,
            target_number=target_number,
            predicted=dict(base_predicted),
        )
        for ot in outcome_types
    ]


async def schedule_outcomes_for_run(
    *,
    run_id: int | None,
    repo_id: int,
    agent_name: str,
    result: "AgentResult",
    context: "AgentContext",
) -> list[int]:
    """
    Create outcome rows for an agent run's measurable actions.

    Returns the list of outcome IDs created (empty list if nothing was scheduled).
    Never raises — all exceptions are logged and swallowed.
    """
    if not run_id or not repo_id:
        return []

    # Only schedule for successful runs — failed runs aren't interventions
    if result.status != "success":
        return []

    try:
        # Resolve predictions: agent-provided or fall back to defaults
        predictions = result.data.get("outcome_predictions") if result.data else None
        if not predictions or not isinstance(predictions, list):
            predictions = _default_predictions_for(agent_name, context, result)

        if not predictions:
            return []

        # Cap at MAX_OUTCOMES_PER_RUN
        if len(predictions) > MAX_OUTCOMES_PER_RUN:
            log.info(
                "outcome_cap_reached",
                run_id=run_id,
                agent=agent_name,
                requested=len(predictions),
                cap=MAX_OUTCOMES_PER_RUN,
            )
            predictions = predictions[:MAX_OUTCOMES_PER_RUN]

        created_ids: list[int] = []

        async with async_session() as session:
            # Procedural dedup: check what's already scheduled for this run
            existing_stmt = select(OutcomeModel.outcome_type).where(
                OutcomeModel.agent_run_id == run_id
            )
            existing = {
                row[0]
                for row in (await session.execute(existing_stmt)).all()
            }

            for pred in predictions:
                outcome_type = pred.get("outcome_type")
                target_type = pred.get("target_type", "unknown")
                target_number = pred.get("target_number")
                predicted_payload = pred.get("predicted") or {}
                delay_override = pred.get("delay_override")

                if not outcome_type:
                    continue
                if outcome_type in existing:
                    # Already scheduled — dedup
                    continue

                # Resolve delay
                if isinstance(delay_override, timedelta):
                    delay = delay_override
                else:
                    delay = OUTCOME_DEFAULT_DELAYS.get(outcome_type, timedelta(hours=24))

                scheduled_for = datetime.utcnow() + delay

                row = OutcomeModel(
                    repo_id=repo_id,
                    agent_run_id=run_id,
                    outcome_type=outcome_type,
                    target_type=target_type,
                    target_number=target_number,
                    predicted=predicted_payload,
                    status=OutcomeStatus.PENDING.value,
                    scheduled_for=scheduled_for,
                )
                session.add(row)
                # Flush to get the ID without committing each row individually
                await session.flush()
                created_ids.append(row.id)
                existing.add(outcome_type)

            await session.commit()

        if created_ids:
            log.info(
                "outcomes_scheduled",
                run_id=run_id,
                agent=agent_name,
                count=len(created_ids),
                outcome_ids=created_ids,
            )

        return created_ids

    except Exception as e:
        log.warning(
            "outcome_schedule_failed",
            run_id=run_id,
            agent=agent_name,
            error=str(e),
            exc_info=True,
        )
        return []
