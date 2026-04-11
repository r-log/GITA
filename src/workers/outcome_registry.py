"""
Outcome registry — the single place that knows which outcome types exist,
how long to wait before checking them, which agents emit which types,
and which checker function handles each type.

Checkers are wired in `outcome_checkers.py` after this file is imported;
`OUTCOME_CHECKERS` is populated lazily to avoid circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Awaitable, Callable, Literal, Optional, TYPE_CHECKING

from src.models.outcome import OutcomeType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ── Default delays per outcome type ────────────────────────────────
# How long to wait before checking whether the intervention worked.

OUTCOME_DEFAULT_DELAYS: dict[str, timedelta] = {
    OutcomeType.SMART_EVAL.value: timedelta(hours=24),
    OutcomeType.CLOSURE_VALIDATION.value: timedelta(hours=48),
    OutcomeType.CHECKLIST_CORRECTION.value: timedelta(hours=48),
    OutcomeType.RISK_WARNING.value: timedelta(hours=72),
    OutcomeType.STALE_NUDGE.value: timedelta(hours=24),
    OutcomeType.DEADLINE_PREDICTION.value: timedelta(hours=24),
}


# ── Default outcomes per agent ──────────────────────────────────────
# When an agent completes without setting its own outcome_predictions,
# the scheduler falls back to these defaults. This means simple agents
# get outcome tracking for free.

AGENT_DEFAULT_OUTCOMES: dict[str, list[str]] = {
    "issue_analyst": [
        OutcomeType.SMART_EVAL.value,
    ],
    "pr_reviewer": [
        OutcomeType.RISK_WARNING.value,
    ],
    "risk_detective": [
        OutcomeType.RISK_WARNING.value,
    ],
    "progress_tracker": [
        OutcomeType.DEADLINE_PREDICTION.value,
    ],
    # Onboarding doesn't get default outcomes — its actions are one-shot
    # and not really measurable as "did the intervention work".
    "onboarding": [],
}


# ── Checker result ──────────────────────────────────────────────────


@dataclass
class CheckerResult:
    """What a checker function returns. The worker wrapper writes this to the DB."""
    status: Literal["success", "partial", "failed"]
    observed: dict = field(default_factory=dict)
    notes: Optional[str] = None


# ── Checker registry ────────────────────────────────────────────────
# Populated lazily by outcome_checkers.py to avoid circular imports.
# The worker looks up checkers here at check time.

CheckerFn = Callable[[int, Optional[int], dict, "AsyncSession"], Awaitable[CheckerResult]]

OUTCOME_CHECKERS: dict[str, CheckerFn] = {}


def register_checker(outcome_type: str, fn: CheckerFn) -> None:
    """Register a checker function for an outcome type."""
    OUTCOME_CHECKERS[outcome_type] = fn


def get_checker(outcome_type: str) -> Optional[CheckerFn]:
    """Get the checker function for an outcome type, or None if not registered."""
    return OUTCOME_CHECKERS.get(outcome_type)
