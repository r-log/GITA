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
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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
    DEDUPED = "deduped"


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
    # Code-writing actions (Week 8): higher bar than any comment path.
    # Bad tests merged into a real repo are harder to unwind than a
    # misphrased issue, so the gate sits at 0.90 by default.
    "create_branch": 0.9,
    "update_file": 0.9,
    "open_pr": 0.9,
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


# Cap a proposed-file preview in the downgrade body so an auto-generated
# 500-line test doesn't wall-of-text the fallback issue.
_PREVIEW_LINES = 40


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

    preview_block = _render_action_preview(original)
    if preview_block:
        lines.extend(preview_block)
        lines.append("")

    lines.append(f"_Confidence: {original.confidence:.2f}_")
    return "\n".join(lines)


def _render_action_preview(original: Decision) -> list[str] | None:
    """Action-specific preview block for downgraded comments.

    Keeps the bridge's evidence list flat (simple one-line bullets) by
    rendering bulkier payload excerpts — the proposed file contents,
    branch/base pair, PR head/base — outside the bullet list. Returns
    ``None`` when the action doesn't define a preview.
    """
    action = original.action
    payload = original.payload

    if action == "update_file":
        path = payload.get("path")
        content = payload.get("content")
        if path is None or content is None:
            return None
        preview = _preview_content(str(content))
        return [
            f"**Proposed content of `{path}`:**",
            "",
            "```python",
            preview,
            "```",
        ]

    if action == "create_branch":
        ref = payload.get("ref")
        base_sha = payload.get("base_sha")
        if not ref or not base_sha:
            return None
        return [
            f"**Proposed branch:** `{ref}` from `{str(base_sha)[:7]}`",
        ]

    if action == "open_pr":
        title = payload.get("title")
        head = payload.get("head")
        base = payload.get("base")
        if not title or not head or not base:
            return None
        return [
            f"**Proposed PR:** `{title}`",
            f"- head: `{head}`",
            f"- base: `{base}`",
        ]

    return None


def _preview_content(content: str) -> str:
    """Cap a long file preview to keep the downgrade body readable."""
    lines = content.splitlines()
    if len(lines) <= _PREVIEW_LINES:
        return content
    head = "\n".join(lines[:_PREVIEW_LINES])
    remaining = len(lines) - _PREVIEW_LINES
    return f"{head}\n# ... ({remaining} more lines)"


def _downgrade_to_comment(original: Decision, reason: str) -> Decision:
    """Convert any non-comment decision into a comment that explains what the
    agent *wanted* to do, carrying the evidence chain.

    If the original decision has no issue number (e.g. ``create_issue``,
    which is creating a new issue and therefore can't downgrade in place),
    the bridge can set ``target['fallback_issue']`` to point at a landing
    issue. Downgrades prefer that over the nonexistent ``target.issue``.
    """
    issue = original.target.get("issue")
    if issue is None:
        issue = original.target.get("fallback_issue")
    return Decision(
        action="comment",
        target={
            "repo": original.target.get("repo"),
            "issue": issue,
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
# Dedupe plumbing — thin wrappers so execute_decision stays readable.
# ---------------------------------------------------------------------------
# Outcomes we never record: either there's nothing to dedupe against (ERROR,
# REJECTED_NO_CLIENT let the next retry try again) or we've already
# short-circuited via the pre-gate (DEDUPED).
_NO_RECORD_OUTCOMES: frozenset[Outcome] = frozenset(
    {Outcome.ERROR, Outcome.REJECTED_NO_CLIENT, Outcome.DEDUPED}
)


def _external_id_from_side_effect(
    side_effect: dict[str, Any] | None,
) -> str | None:
    """Extract a GitHub-side identifier from a client's return value, if any.

    Clients are free to return whatever shape they want — the framework just
    copies out anything that looks like an ``id`` or ``external_id`` so it
    can land in ``agent_actions.external_id`` for traceability. Missing is
    fine; the column is nullable.
    """
    if not side_effect:
        return None
    for key in ("id", "external_id", "issue_number", "comment_id"):
        value = side_effect.get(key)
        if value is not None:
            return str(value)
    return None


async def _dedupe_pregate(
    decision: Decision,
    mode: WriteMode,
    session: "AsyncSession | None",
    agent: str | None,
) -> DecisionResult | None:
    """Check ``agent_actions`` for a prior run of this decision.

    Returns a :class:`DecisionResult` with ``outcome=DEDUPED`` if the
    signature is already in the table, else ``None`` to signal "fall through
    to the normal gate."

    Imports the dedupe module lazily to keep a framework-level import of
    SQLAlchemy out of the hot path for shadow-without-session callers.
    """
    if session is None:
        return None

    # Lazy import — avoids a framework-level dependency on the dedupe module
    # when callers don't use dedupe (existing test suite pattern).
    from gita.agents.dedupe import check_signature, compute_signature

    try:
        signature = compute_signature(decision)
    except ValueError as exc:
        # Decision shape is invalid for signature computation (missing repo,
        # unknown action). Don't treat this as a framework error — let the
        # threshold / downgrade flow handle whatever is malformed. Dedupe is
        # a best-effort optimization; it's OK to skip here.
        logger.warning(
            "dedupe_signature_failed action=%s reason=%s",
            decision.action,
            exc,
        )
        return None

    existing = await check_signature(session, decision, agent=agent)  # type: ignore[arg-type]
    if existing is None:
        return None

    logger.info(
        "decision_deduped action=%s repo=%s agent=%s "
        "previous_outcome=%s previous_external_id=%s sig=%s",
        decision.action,
        decision.target.get("repo"),
        agent,
        existing.outcome,
        existing.external_id,
        signature[:12],
    )
    return DecisionResult(
        decision=decision,
        mode=mode,
        outcome=Outcome.DEDUPED,
        executed=False,
        side_effect={
            "deduped": True,
            "agent_action_id": str(existing.id),
            "external_id": existing.external_id,
            "previous_outcome": existing.outcome,
            "signature": signature,
        },
    )


async def _record_and_finalize(
    result: DecisionResult,
    session: "AsyncSession | None",
    agent: str | None,
) -> DecisionResult:
    """Persist ``result`` to ``agent_actions`` if appropriate and return it.

    Recording rules:
    - No session? Skip recording — the caller opted out of dedupe.
    - Outcome in ``_NO_RECORD_OUTCOMES``? Skip — ``ERROR`` / ``REJECTED_NO_CLIENT``
      must be retriable; ``DEDUPED`` is already recorded from a prior run.
    - In shadow mode? Always record — the plan requires shadow-to-shadow
      dedupe so the same decision can't be logged twice.
    - Otherwise: record only if the side effect actually landed (``executed``).

    Race handling: a concurrent writer can beat us between the pre-gate and
    this insert, which triggers a unique-constraint violation. When that
    happens we roll back and return a fresh ``DEDUPED`` result — the other
    writer's side effect is authoritative.
    """
    if session is None:
        return result

    if result.outcome in _NO_RECORD_OUTCOMES:
        return result

    should_record = result.mode == WriteMode.SHADOW or result.executed
    if not should_record:
        return result

    from gita.agents.dedupe import record_action

    try:
        row = await record_action(
            session,
            result.decision,
            agent=agent,  # type: ignore[arg-type]
            outcome=result.outcome.value,
            external_id=_external_id_from_side_effect(result.side_effect),
        )
    except IntegrityError:
        # Concurrent race — another caller recorded this signature between
        # our pre-gate check and now. Roll back so the session is usable,
        # then report the outcome the other writer would see.
        await session.rollback()
        logger.info(
            "decision_deduped_via_race action=%s repo=%s agent=%s",
            result.decision.action,
            result.decision.target.get("repo"),
            agent,
        )
        return DecisionResult(
            decision=result.decision,
            mode=result.mode,
            outcome=Outcome.DEDUPED,
            executed=False,
            side_effect={"deduped": True, "via": "integrity_race"},
        )

    # Thread the agent_action row id into side_effect so the CLI can surface
    # it in output without re-querying the DB.
    tagged_side_effect: dict[str, Any] = dict(result.side_effect or {})
    tagged_side_effect["agent_action_id"] = str(row.id)
    return replace(result, side_effect=tagged_side_effect)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
async def execute_decision(
    decision: Decision,
    *,
    mode: WriteMode,
    client: ActionClient | None = None,
    thresholds: dict[str, float] | None = None,
    session: "AsyncSession | None" = None,
    agent: str | None = None,
) -> DecisionResult:
    """Route a decision through the confidence + write-mode gates.

    ``session`` and ``agent`` together enable automatic dedupe against the
    ``agent_actions`` table. They're optional to preserve the Week 2 call
    shape for tests that don't care about dedupe; when ``session`` is
    provided, ``agent`` is required (we need the scoping key). When
    ``session`` is omitted, the decision flows through the existing three
    gates unchanged.

    The dedupe signature is computed from the *original* decision, never
    from a downgraded intermediate. This means "create_issue X under
    comment mode" and "create_issue X under full mode" both hash to the
    same row, so a run that posted a downgrade explanation in comment mode
    will dedupe a later run in full mode against the same original intent.
    Simpler to reason about than per-execution-shape signatures, and
    preserves the "one side effect per original decision" property.
    """
    # 0. Caller validation — require agent whenever session is passed.
    if session is not None and not agent:
        raise ValueError(
            "execute_decision: 'agent' is required when 'session' is "
            "provided (needed to scope agent_actions rows per agent)"
        )

    # 1. Threshold lookup (raises on unknown action — fail loud, not silent).
    threshold = get_threshold(decision.action, thresholds)

    # 2. Dedupe pre-gate — short-circuit if this decision has already run.
    deduped = await _dedupe_pregate(decision, mode, session, agent)
    if deduped is not None:
        return deduped

    # 3. Low confidence? Downgrade to comment explaining intent.
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
        result = DecisionResult(
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
        return await _record_and_finalize(result, session, agent)

    # 4. Comment mode with a non-comment action? Downgrade.
    if mode == WriteMode.COMMENT and decision.action not in COMMENT_ACTIONS:
        downgraded = _downgrade_to_comment(
            decision,
            reason=f"WRITE_MODE=comment disallows action `{decision.action}`",
        )
        executed, side_effect, error = await _execute_side_effect(
            downgraded, mode, client
        )
        result = DecisionResult(
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
        return await _record_and_finalize(result, session, agent)

    # 5. Passes all gates → execute as requested (or skip in shadow).
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

    result = DecisionResult(
        decision=decision,
        mode=mode,
        outcome=outcome,
        executed=executed,
        side_effect=side_effect,
        error=error,
    )
    return await _record_and_finalize(result, session, agent)
