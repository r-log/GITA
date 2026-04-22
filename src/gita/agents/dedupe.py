"""Automatic dedupe for Decision objects via the ``agent_actions`` table.

The framework's idempotency guarantee is "the same action against the same
target yields exactly one side-effect, even across re-runs." That guarantee
has two halves:

1. **Signature computation** — a deterministic hex hash over the identifying
   payload of a decision. Shapes are action-specific and deliberately targeted
   at the fields that make two decisions "the same" (e.g. for ``create_issue``
   the title is the identity, not the full body).

2. **`agent_actions` round-trip** — check for a row matching
   ``(repo_name, agent, action, signature)`` before executing, insert a new
   row after executing. The table has a unique constraint on those four
   columns so concurrent inserts raise ``IntegrityError`` which the gate
   converts to ``Outcome.DEDUPED``.

The plumbing into ``execute_decision`` lands in Day 2 of Week 3. Day 1 ships
the pure signature function and the DB helpers with unit tests so the gate
can bolt them on cleanly.
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.agents.decisions import Decision
from gita.db.models import AgentAction

logger = logging.getLogger(__name__)

# Truncate free-form text fields before hashing so the signature material
# stays short. 200 characters is enough to disambiguate distinct bodies
# without making a tiny typo a new "identity."
_BODY_CAP_CHARS = 200


# ---------------------------------------------------------------------------
# Signature computation
# ---------------------------------------------------------------------------
def compute_signature(decision: Decision) -> str:
    """Compute a deterministic sha256 hex signature for a decision.

    The signature is the identity of the *intended side effect*, not of the
    specific Decision object. Two Decisions with the same action, same repo,
    same target, and same identifying payload fields yield the same signature
    — that's the whole point.

    Raises ``ValueError`` for unknown actions (fail loud, same pattern as
    ``get_threshold``) or missing ``target.repo`` (which the shapes all need).
    """
    repo = _repo_for_signature(decision)
    action = decision.action

    if action == "create_issue":
        # Prefer _signature_keys (sorted file:line citations) over the title.
        # Title-based signatures miss when the LLM rephrases equivalent
        # milestones between runs (observed Day 7 flip-back: 2/5 title
        # drifts at temperature=0). Citation-based signatures dedupe on
        # the *underlying findings* regardless of how the LLM phrases
        # the milestone title. The bridge pre-computes _signature_keys;
        # fall back to title for manually-built decisions.
        sig_keys = decision.payload.get("_signature_keys")
        if sig_keys and isinstance(sig_keys, list):
            keys_str = "\n".join(sorted(str(k) for k in sig_keys))
            material = f"{repo}\ncreate_issue\n{keys_str}"
        else:
            title = str(decision.payload.get("title", "")).strip().lower()
            material = f"{repo}\ncreate_issue\n{title}"
    elif action == "comment":
        issue = decision.target.get("issue")
        body = str(decision.payload.get("body", "")).strip()[:_BODY_CAP_CHARS]
        material = f"{repo}\ncomment\n{issue}\n{body}"
    elif action == "close_issue":
        issue = decision.target.get("issue")
        material = f"{repo}\nclose_issue\n{issue}"
    elif action == "edit_issue":
        issue = decision.target.get("issue")
        title = str(decision.payload.get("title", "")).strip()
        body = str(decision.payload.get("body", "")).strip()[:_BODY_CAP_CHARS]
        material = f"{repo}\nedit_issue\n{issue}\n{title}\n{body}"
    elif action == "add_label":
        issue = decision.target.get("issue")
        labels = decision.payload.get("labels") or []
        normalized = ",".join(sorted(str(label) for label in labels if label))
        material = f"{repo}\nadd_label\n{issue}\n{normalized}"
    elif action == "remove_label":
        issue = decision.target.get("issue")
        label = str(decision.payload.get("label", ""))
        material = f"{repo}\nremove_label\n{issue}\n{label}"
    elif action == "create_branch":
        # Identity = (ref name, base SHA). Retrying the same branch from the
        # same source commit is a no-op; branching the same name from a
        # different SHA is a *different* intent (fresh content).
        ref_name = str(decision.payload.get("ref", ""))
        base_sha = str(decision.payload.get("base_sha", ""))
        material = f"{repo}\ncreate_branch\n{ref_name}\n{base_sha}"
    elif action == "update_file":
        # Identity = (branch, path, content-hash). Writing the same bytes to
        # the same path on the same branch dedupes even across restarts; a
        # different path or branch or content is a new decision.
        branch = str(decision.payload.get("branch", ""))
        path = str(decision.payload.get("path", ""))
        raw_content = decision.payload.get("content", "") or ""
        content_hash = hashlib.sha256(
            str(raw_content).encode("utf-8")
        ).hexdigest()
        material = (
            f"{repo}\nupdate_file\n{branch}\n{path}\n{content_hash}"
        )
    elif action == "open_pr":
        # Identity = (head, base) branch pair. GitHub itself rejects opening
        # two PRs with the same head→base pair, so we dedupe on that too.
        head = str(decision.payload.get("head", ""))
        base = str(decision.payload.get("base", ""))
        material = f"{repo}\nopen_pr\n{head}\n{base}"
    else:
        raise ValueError(
            f"no signature shape configured for action {action!r}"
        )

    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _repo_for_signature(decision: Decision) -> str:
    """Extract + normalize the repo identifier from a decision.

    Normalization is case-insensitive (GitHub repo names are) and strips
    whitespace. This runs on both sides of the check — compute_signature and
    check_signature — so ``Owner/Repo`` and ``owner/repo`` collapse to the
    same row.
    """
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
) -> AgentAction | None:
    """Return an existing ``agent_actions`` row if this decision has been
    recorded before, otherwise ``None``.

    Lookup key is ``(repo_name, agent, action, signature)`` — matching the
    table's unique constraint. The signature is computed inline from
    ``decision``, so callers don't need to hash anything themselves.
    """
    signature = compute_signature(decision)
    repo_name = _repo_for_signature(decision)

    stmt = (
        select(AgentAction)
        .where(AgentAction.repo_name == repo_name)
        .where(AgentAction.agent == agent)
        .where(AgentAction.action == decision.action)
        .where(AgentAction.signature == signature)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def record_action(
    session: AsyncSession,
    decision: Decision,
    *,
    agent: str,
    outcome: str,
    external_id: str | None = None,
) -> AgentAction:
    """Persist a decision-execution result into ``agent_actions``.

    Caller is responsible for committing the session. This function only
    flushes so ``row.id`` is populated on return.

    Raises ``sqlalchemy.exc.IntegrityError`` on unique-constraint violation.
    The gate layer (Day 2) catches that and maps it to ``Outcome.DEDUPED``.
    """
    signature = compute_signature(decision)
    repo_name = _repo_for_signature(decision)

    row = AgentAction(
        repo_name=repo_name,
        agent=agent,
        action=decision.action,
        signature=signature,
        external_id=external_id,
        outcome=outcome,
        confidence=decision.confidence,
        evidence=list(decision.evidence),
    )
    session.add(row)
    await session.flush()
    logger.info(
        "agent_action_recorded agent=%s action=%s outcome=%s repo=%s sig=%s",
        agent,
        decision.action,
        outcome,
        repo_name,
        signature[:12],
    )
    return row
