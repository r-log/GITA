"""Confidence-gated write framework.

Every agent write flows through a ``Decision`` object and ``execute_decision``.
The framework enforces two invariants *at framework level*, not prompt level:

1. **Confidence threshold per action.** If ``decision.confidence`` is below
   the configured threshold for ``decision.action``, the action is
   auto-downgraded to a comment on the target issue explaining the *intended*
   action and its evidence chain. No silent writes.

2. **Write mode gate.** An environment flag controls what actions are allowed
   to actually execute:

   - ``shadow`` — log the decision, never call the client. **Default.** Used
     for development and for any session that hasn't been explicitly approved
     to write to GitHub.
   - ``comment`` — execute ``comment`` actions; downgrade every other action
     to a comment.
   - ``full`` — execute anything that passes its threshold. Not flipped on in
     Week 2.

An ``ActionClient`` is a tiny Protocol with a single ``execute(decision)``
method. Implementations (the real GitHub client in Day 4, fakes in tests)
dispatch on ``decision.action`` themselves. Shadow mode never touches the
client.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class WriteMode(str, Enum):
    SHADOW = "shadow"
    COMMENT = "comment"
    FULL = "full"


class Outcome(str, Enum):
    """Terminal state of a decision passed through ``execute_decision``."""

    EXECUTED = "executed"
    SHADOW_LOGGED = "shadow_logged"
    DOWNGRADED_LOW_CONFIDENCE = "downgraded_low_confidence"
    DOWNGRADED_WRITE_MODE = "downgraded_write_mode"
    REJECTED_NO_CLIENT = "rejected_no_client"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Default thresholds
# ---------------------------------------------------------------------------
# Keys are action names. The value is the minimum confidence for the action
# to execute as-requested. Below this, the action auto-downgrades to a comment.
# Tune after Day 6 once we've seen the first real onboarding outputs.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "comment": 0.3,
    "label": 0.5,
    "edit_issue": 0.75,
    "create_issue": 0.7,
    "close_issue": 0.8,
}

# Actions that produce comments (always allowed in WriteMode.COMMENT).
COMMENT_ACTIONS: frozenset[str] = frozenset({"comment"})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    """A declaration of intent: *"I want to do X with evidence Y at confidence Z."*

    The framework decides whether to actually execute it.
    """

    action: str
    target: dict[str, Any]           # e.g. {"repo": "owner/name", "issue": 42}
    payload: dict[str, Any]          # action-specific body/title/label/etc.
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not self.action:
            raise ValueError("Decision.action cannot be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"Decision.confidence must be in [0.0, 1.0], got {self.confidence}"
            )


@dataclass
class DecisionResult:
    """Terminal state of a decision after routing through the gate."""

    decision: Decision
    mode: WriteMode
    outcome: Outcome
    executed: bool = False
    downgrade_reason: str | None = None
    error: str | None = None
    side_effect: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Client protocol
# ---------------------------------------------------------------------------
class ActionClient(Protocol):
    """A client that can execute a Decision.

    The real implementation lands in Day 4 (``gita.github.client``); tests use
    ``FakeActionClient`` below.
    """

    async def execute(self, decision: Decision) -> dict[str, Any]:
        """Perform the action. Return a dict describing the side effect."""
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_threshold(
    action: str, thresholds: dict[str, float] | None = None
) -> float:
    """Look up the threshold for an action. Raises KeyError on unknown actions.

    Unknown actions intentionally raise instead of defaulting — if an agent
    introduces a new action type, we want it to fail loudly until somebody
    configures a threshold for it.
    """
    table = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
    if action not in table:
        raise KeyError(f"no threshold configured for action {action!r}")
    return table[action]


def _render_downgrade_body(original: Decision, reason: str) -> str:
    """Build the body of a downgraded-to-comment message."""
    lines = [
        f"**Intended action:** `{original.action}`",
        "",
        f"**Why this is a comment instead:** {reason}",
        "",
    ]
    if original.evidence:
        lines.append("**Evidence:**")
        for ev in original.evidence:
            lines.append(f"- {ev}")
        lines.append("")
    lines.append(f"_Confidence: {original.confidence:.2f}_")
    return "\n".join(lines)


def _downgrade_to_comment(original: Decision, reason: str) -> Decision:
    """Convert any non-comment decision into a comment that explains what the
    agent *wanted* to do, carrying the evidence chain.
    """
    return Decision(
        action="comment",
        target={
            "repo": original.target.get("repo"),
            "issue": original.target.get("issue"),
        },
        payload={"body": _render_downgrade_body(original, reason)},
        evidence=original.evidence,
        # Downgraded comments get a ceiling confidence so they can't recurse.
        confidence=1.0,
    )


async def _execute_side_effect(
    decision: Decision,
    mode: WriteMode,
    client: ActionClient | None,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    """Run the client (or skip in shadow). Returns (executed, side_effect, error)."""
    if mode == WriteMode.SHADOW:
        logger.info(
            "shadow_decision action=%s target=%s confidence=%.2f evidence=%d",
            decision.action,
            decision.target,
            decision.confidence,
            len(decision.evidence),
        )
        return (False, None, None)

    if client is None:
        return (False, None, "no ActionClient provided")

    try:
        side_effect = await client.execute(decision)
    except Exception as exc:  # noqa: BLE001 — framework intentionally catches
        logger.warning(
            "decision_execution_failed action=%s error=%s",
            decision.action,
            exc,
        )
        return (False, None, str(exc))
    return (True, side_effect, None)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
async def execute_decision(
    decision: Decision,
    *,
    mode: WriteMode,
    client: ActionClient | None = None,
    thresholds: dict[str, float] | None = None,
) -> DecisionResult:
    """Route a decision through the confidence + write-mode gates."""
    # 1. Threshold lookup (raises on unknown action — fail loud, not silent).
    threshold = get_threshold(decision.action, thresholds)

    # 2. Low confidence? Downgrade to comment explaining intent.
    if decision.confidence < threshold:
        downgraded = _downgrade_to_comment(
            decision,
            reason=(
                f"confidence {decision.confidence:.2f} is below threshold "
                f"{threshold:.2f} for action `{decision.action}`"
            ),
        )
        executed, side_effect, error = await _execute_side_effect(
            downgraded, mode, client
        )
        return DecisionResult(
            decision=decision,
            mode=mode,
            outcome=Outcome.DOWNGRADED_LOW_CONFIDENCE,
            executed=executed,
            downgrade_reason=(
                f"confidence {decision.confidence:.2f} below threshold "
                f"{threshold:.2f}"
            ),
            side_effect=side_effect,
            error=error,
        )

    # 3. Comment mode with a non-comment action? Downgrade.
    if mode == WriteMode.COMMENT and decision.action not in COMMENT_ACTIONS:
        downgraded = _downgrade_to_comment(
            decision,
            reason=f"WRITE_MODE=comment disallows action `{decision.action}`",
        )
        executed, side_effect, error = await _execute_side_effect(
            downgraded, mode, client
        )
        return DecisionResult(
            decision=decision,
            mode=mode,
            outcome=Outcome.DOWNGRADED_WRITE_MODE,
            executed=executed,
            downgrade_reason=(
                f"WRITE_MODE=comment, action was `{decision.action}`"
            ),
            side_effect=side_effect,
            error=error,
        )

    # 4. Passes all gates → execute as requested (or skip in shadow).
    executed, side_effect, error = await _execute_side_effect(
        decision, mode, client
    )

    if executed:
        outcome = Outcome.EXECUTED
    elif mode == WriteMode.SHADOW:
        outcome = Outcome.SHADOW_LOGGED
    elif error == "no ActionClient provided":
        outcome = Outcome.REJECTED_NO_CLIENT
    else:
        outcome = Outcome.ERROR

    return DecisionResult(
        decision=decision,
        mode=mode,
        outcome=outcome,
        executed=executed,
        side_effect=side_effect,
        error=error,
    )
