"""Idempotency for Decisions via the ``agent_actions`` table.

The guarantee is "the same intended side effect, against the same
target, happens once — across re-runs, retries and concurrent workers."
It has three parts:

1. **Identity.** ``compute_signature`` hashes what makes two decisions
   "the same". A bridge that knows the identity should say so with
   ``Decision.idempotency_key`` (e.g. ``pr-review:42:<head sha>``);
   otherwise each action has a fallback shape over its payload.
   Rendered text is a poor identity — two clean reviews of different
   commits produce byte-identical comment bodies.

2. **Liveness.** Rows are partitioned by ``live``: shadow-mode rows
   (nothing reached GitHub) never satisfy a live dedupe check, so
   observing a decision in shadow can't suppress it once writes are
   switched on.

3. **Reservation.** ``execute_decision`` inserts the row *before* the
   side effect, inside a savepoint. The unique constraint on
   ``(repo_name, agent, action, signature, live)`` makes a concurrent
   writer block on its insert until the first transaction finishes,
   then fail with ``IntegrityError`` — so two workers can't both post.
   The row is removed again if the side effect fails, so retries work.
   Semantics on a crash between the GitHub call and the commit are
   at-least-once: the reservation rolls back and a retry repeats the
   call.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.decisions import Decision
from gita.db.models import AgentAction

logger = logging.getLogger(__name__)

# Fallback shapes truncate free text so a typo isn't a new identity.
_BODY_CAP_CHARS = 200


# ---------------------------------------------------------------------------
# Fallback signature shapes (used when no idempotency_key is given)
# ---------------------------------------------------------------------------
def _sig_create_issue(d: Decision) -> str:
    # Citation keys (sorted file:line) survive the model rephrasing a
    # milestone title between runs; the title is a last resort.
    keys = d.payload.get("_signature_keys")
    if keys and isinstance(keys, list):
        return "\n".join(sorted(str(k) for k in keys))
    return str(d.payload.get("title", "")).strip().lower()


def _sig_comment(d: Decision) -> str:
    body = str(d.payload.get("body", "")).strip()[:_BODY_CAP_CHARS]
    return f"{d.target.get('issue')}\n{body}"


def _sig_close_issue(d: Decision) -> str:
    return str(d.target.get("issue"))


def _sig_edit_issue(d: Decision) -> str:
    title = str(d.payload.get("title", "")).strip()
    body = str(d.payload.get("body", "")).strip()[:_BODY_CAP_CHARS]
    return f"{d.target.get('issue')}\n{title}\n{body}"


def _sig_add_label(d: Decision) -> str:
    labels = d.payload.get("labels") or []
    return f"{d.target.get('issue')}\n" + ",".join(sorted(str(x) for x in labels if x))


def _sig_remove_label(d: Decision) -> str:
    return f"{d.target.get('issue')}\n{d.payload.get('label', '')}"


def _sig_create_branch(d: Decision) -> str:
    # Same branch name from a different base SHA is a different intent.
    return f"{d.payload.get('ref', '')}\n{d.payload.get('base_sha', '')}"


def _sig_update_file(d: Decision) -> str:
    content = str(d.payload.get("content", "") or "")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"{d.payload.get('branch', '')}\n{d.payload.get('path', '')}\n{digest}"


def _sig_open_pr(d: Decision) -> str:
    # GitHub itself rejects a second open PR for the same head→base pair.
    return f"{d.payload.get('head', '')}\n{d.payload.get('base', '')}"


SIGNATURE_SHAPES: dict[str, Callable[[Decision], str]] = {
    "comment": _sig_comment,
    "create_issue": _sig_create_issue,
    "close_issue": _sig_close_issue,
    "edit_issue": _sig_edit_issue,
    "add_label": _sig_add_label,
    "remove_label": _sig_remove_label,
    "create_branch": _sig_create_branch,
    "update_file": _sig_update_file,
    "open_pr": _sig_open_pr,
}


# ---------------------------------------------------------------------------
# Signature computation
# ---------------------------------------------------------------------------
def compute_signature(decision: Decision) -> str:
    """Deterministic sha256 hex identity of the decision's intended effect.

    Raises ``ValueError`` for an unknown action or a missing
    ``target.repo`` — fail loud, same as ``get_threshold``.
    """
    repo = _repo_for_signature(decision)
    action = decision.action
    if decision.idempotency_key:
        material = f"{repo}\n{action}\nkey:{decision.idempotency_key}"
    else:
        shape = SIGNATURE_SHAPES.get(action)
        if shape is None:
            raise ValueError(f"no signature shape configured for action {action!r}")
        material = f"{repo}\n{action}\n{shape(decision)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _repo_for_signature(decision: Decision) -> str:
    """Normalized ``target.repo`` (GitHub repo names are case-insensitive)."""
    repo = decision.target.get("repo")
    if not repo:
        raise ValueError(
            "decision.target must include 'repo' for signature "
            f"computation; got target={decision.target!r}"
        )
    return str(repo).strip().lower()


# ---------------------------------------------------------------------------
# DB round-trip
# ---------------------------------------------------------------------------
async def check_signature(
    session: AsyncSession,
    decision: Decision,
    *,
    agent: str,
    live: bool = True,
) -> AgentAction | None:
    """The existing row for this decision's identity, if any.

    ``live`` selects the partition: a live run is only deduplicated by
    side effects that actually happened, a shadow run by earlier shadow
    observations.
    """
    stmt = (
        select(AgentAction)
        .where(AgentAction.repo_name == _repo_for_signature(decision))
        .where(AgentAction.agent == agent)
        .where(AgentAction.action == decision.action)
        .where(AgentAction.signature == compute_signature(decision))
        .where(AgentAction.live.is_(live))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_action(
    session: AsyncSession,
    decision: Decision,
    *,
    agent: str,
    outcome: str,
    external_id: str | None = None,
    live: bool = True,
) -> AgentAction:
    """Insert an ``agent_actions`` row and flush (caller commits).

    Raises ``sqlalchemy.exc.IntegrityError`` when the identity already
    exists in this partition. Callers that may race should wrap the call
    in ``session.begin_nested()`` so the failure doesn't poison the
    enclosing transaction.
    """
    row = AgentAction(
        repo_name=_repo_for_signature(decision),
        agent=agent,
        action=decision.action,
        signature=compute_signature(decision),
        external_id=external_id,
        outcome=outcome,
        confidence=decision.confidence,
        evidence=list(decision.evidence),
        live=live,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "agent_action_recorded agent=%s action=%s outcome=%s live=%s repo=%s sig=%s",
        agent,
        decision.action,
        outcome,
        live,
        row.repo_name,
        row.signature[:12],
    )
    return row
