"""
Outcome checker functions — pure "look at world model, return verdict".

Each checker is a coroutine with the signature:
    async def check_X(repo_id, target_number, predicted, session) -> CheckerResult

Checkers must NOT:
- Call the GitHub API (too slow, can't be replayed offline)
- Mutate the outcome row (that's the worker's job)
- Raise exceptions for expected "missing data" cases (return `failed` with notes)

Checkers MAY:
- Read any world model table via the provided AsyncSession
- Look at event history in `events`, issue/PR state in `issues`/`pull_requests`,
  commit history in `commits`, comment history in `comments`
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.issue import IssueModel
from src.models.pull_request import PullRequestModel
from src.models.commit import CommitModel
from src.models.comment import CommentModel
from src.models.event import EventModel
from src.models.outcome import OutcomeType
from src.workers.outcome_registry import CheckerResult, register_checker


# ── Helpers ─────────────────────────────────────────────────────────


def _body_hash(body: Optional[str]) -> str:
    """SHA256 of an issue body, normalized to empty string on None."""
    return hashlib.sha256((body or "").encode("utf-8", errors="replace")).hexdigest()


async def _get_issue(session: AsyncSession, repo_id: int, number: int) -> Optional[IssueModel]:
    result = await session.execute(
        select(IssueModel).where(
            IssueModel.repo_id == repo_id,
            IssueModel.github_number == number,
        )
    )
    return result.scalar_one_or_none()


async def _get_pr(session: AsyncSession, repo_id: int, number: int) -> Optional[PullRequestModel]:
    result = await session.execute(
        select(PullRequestModel).where(
            PullRequestModel.repo_id == repo_id,
            PullRequestModel.github_number == number,
        )
    )
    return result.scalar_one_or_none()


# ── Checker: smart_eval ─────────────────────────────────────────────


async def check_smart_eval(
    repo_id: int,
    target_number: Optional[int],
    predicted: dict,
    session: AsyncSession,
) -> CheckerResult:
    """
    Did the S.M.A.R.T. evaluation advice land? Check if the issue body changed
    or recommended labels were added after we posted the evaluation comment.
    """
    if target_number is None:
        return CheckerResult(status="failed", notes="no target_number")

    issue = await _get_issue(session, repo_id, target_number)
    if not issue:
        return CheckerResult(
            status="failed",
            notes=f"issue #{target_number} not in world model",
        )

    initial_hash = predicted.get("initial_body_hash")
    initial_labels = set(predicted.get("initial_labels") or [])
    recommended_labels = set(predicted.get("recommended_labels") or [])

    current_hash = _body_hash(issue.body)
    current_labels = {
        (l.get("name") if isinstance(l, dict) else l)
        for l in (issue.labels or [])
    }

    body_changed = initial_hash and current_hash != initial_hash
    labels_added = bool(recommended_labels & current_labels - initial_labels)
    labels_changed = current_labels != initial_labels

    observed = {
        "body_changed": bool(body_changed),
        "labels_changed": labels_changed,
        "recommended_labels_applied": sorted(recommended_labels & current_labels),
        "current_labels": sorted(current_labels),
    }

    if body_changed and labels_added:
        return CheckerResult(status="success", observed=observed,
                             notes="Both body and recommended labels updated")
    if body_changed or labels_added:
        return CheckerResult(status="success", observed=observed,
                             notes="Body or recommended labels updated")
    if labels_changed:
        return CheckerResult(status="partial", observed=observed,
                             notes="Labels changed but not as recommended")
    return CheckerResult(status="failed", observed=observed,
                         notes="No meaningful changes since evaluation")


# ── Checker: closure_validation ─────────────────────────────────────


async def check_closure_validation(
    repo_id: int,
    target_number: Optional[int],
    predicted: dict,
    session: AsyncSession,
) -> CheckerResult:
    """
    Did the issue stay closed? Check for reopen events after the closure.
    """
    if target_number is None:
        return CheckerResult(status="failed", notes="no target_number")

    issue = await _get_issue(session, repo_id, target_number)
    if not issue:
        return CheckerResult(
            status="failed",
            notes=f"issue #{target_number} not in world model",
        )

    observed = {
        "current_state": issue.state,
        "closed_at": issue.closed_at.isoformat() if issue.closed_at else None,
    }

    # If it's still closed, check for reopen events in between
    if issue.state == "closed":
        # Look for any reopened events after scheduled closure
        events_result = await session.execute(
            select(EventModel).where(
                EventModel.repo_id == repo_id,
                EventModel.event_type == "issues",
                EventModel.target_number == target_number,
                EventModel.action == "reopened",
            ).order_by(desc(EventModel.received_at)).limit(5)
        )
        reopens = events_result.scalars().all()
        observed["reopen_count"] = len(reopens)

        if reopens:
            return CheckerResult(
                status="partial",
                observed=observed,
                notes=f"Closed but was reopened {len(reopens)} time(s) in between",
            )
        return CheckerResult(
            status="success",
            observed=observed,
            notes="Issue stayed closed",
        )

    # Currently open — was it reopened after being closed?
    return CheckerResult(
        status="failed",
        observed=observed,
        notes=f"Issue is currently {issue.state}, was expected to stay closed",
    )


# ── Checker: checklist_correction ───────────────────────────────────


async def check_checklist_correction(
    repo_id: int,
    target_number: Optional[int],
    predicted: dict,
    session: AsyncSession,
) -> CheckerResult:
    """
    Did the checklist correction stick? Compare the current body hash to what
    we left it at. If it changed, check if our corrections are still in place.
    """
    if target_number is None:
        return CheckerResult(status="failed", notes="no target_number")

    issue = await _get_issue(session, repo_id, target_number)
    if not issue:
        return CheckerResult(
            status="failed",
            notes=f"issue #{target_number} not in world model",
        )

    corrected_hash = predicted.get("corrected_body_hash")
    current_hash = _body_hash(issue.body)

    observed = {
        "body_changed_since_correction": bool(corrected_hash and current_hash != corrected_hash),
        "current_hash": current_hash,
    }

    if not corrected_hash:
        return CheckerResult(
            status="partial",
            observed=observed,
            notes="No corrected_body_hash in predicted — can't verify",
        )

    if current_hash == corrected_hash:
        return CheckerResult(
            status="success",
            observed=observed,
            notes="Checklist correction still in place",
        )

    # Body changed — did the corrections get reverted?
    # Simple heuristic: look for the specific checklist markers we corrected
    corrected_items = predicted.get("corrected_items", [])
    body = issue.body or ""
    still_unchecked = sum(1 for item in corrected_items if f"- [ ] {item}" in body)

    if still_unchecked == len(corrected_items) and corrected_items:
        return CheckerResult(
            status="success",
            observed={**observed, "still_unchecked": still_unchecked},
            notes="Body changed but corrections are still intact",
        )
    return CheckerResult(
        status="failed",
        observed={**observed, "still_unchecked": still_unchecked},
        notes=f"Only {still_unchecked}/{len(corrected_items)} corrections still intact",
    )


# ── Checker: risk_warning ───────────────────────────────────────────


async def check_risk_warning(
    repo_id: int,
    target_number: Optional[int],
    predicted: dict,
    session: AsyncSession,
) -> CheckerResult:
    """
    Did the risk warning matter? Check PR state and whether warned files
    were addressed in follow-up commits.
    """
    if target_number is None:
        return CheckerResult(status="failed", notes="no target_number")

    pr = await _get_pr(session, repo_id, target_number)
    if not pr:
        return CheckerResult(
            status="failed",
            notes=f"PR #{target_number} not in world model",
        )

    severity = predicted.get("severity", "unknown")
    warned_files = set(predicted.get("file_paths_warned") or [])

    observed = {
        "pr_state": pr.state,
        "merged": pr.merged_at is not None,
        "severity": severity,
    }

    # PR closed without merge → warning was respected (author didn't push through)
    if pr.state == "closed" and pr.merged_at is None:
        return CheckerResult(
            status="success",
            observed=observed,
            notes="PR closed without merge — warning respected",
        )

    # PR still open → can't judge yet
    if pr.state == "open":
        return CheckerResult(
            status="partial",
            observed=observed,
            notes="PR still open — outcome not determined",
        )

    # PR merged → did follow-up commits address the warned files?
    if warned_files:
        commits_result = await session.execute(
            select(CommitModel).where(
                CommitModel.repo_id == repo_id,
                CommitModel.committed_at >= (pr.github_created_at or datetime.min),
            ).order_by(desc(CommitModel.committed_at)).limit(50)
        )
        commits = commits_result.scalars().all()
        files_touched_after = set()
        for c in commits:
            files_touched_after.update(c.files_modified or [])
            files_touched_after.update(c.files_added or [])
        addressed = warned_files & files_touched_after
        observed["warned_files_touched_after"] = sorted(addressed)

        if addressed:
            return CheckerResult(
                status="partial",
                observed=observed,
                notes=f"Merged, but {len(addressed)} warned files were addressed in follow-up commits",
            )

    return CheckerResult(
        status="failed",
        observed=observed,
        notes="PR merged despite warning, no follow-up fixes to warned files detected",
    )


# ── Checker: stale_nudge ────────────────────────────────────────────


async def check_stale_nudge(
    repo_id: int,
    target_number: Optional[int],
    predicted: dict,
    session: AsyncSession,
) -> CheckerResult:
    """
    Did the stale nudge wake the PR up? Look for new activity (commits,
    comments, review events) after the nudge was posted.
    """
    if target_number is None:
        return CheckerResult(status="failed", notes="no target_number")

    nudged_at_str = predicted.get("nudged_at")
    if not nudged_at_str:
        return CheckerResult(status="failed", notes="no nudged_at in predicted")

    try:
        nudged_at = datetime.fromisoformat(nudged_at_str.replace("Z", "+00:00"))
        if nudged_at.tzinfo is not None:
            nudged_at = nudged_at.replace(tzinfo=None)
    except (ValueError, TypeError):
        return CheckerResult(status="failed", notes=f"bad nudged_at: {nudged_at_str}")

    # Look for any activity on this PR after the nudge
    observed = {"nudged_at": nudged_at_str, "activity_found": []}

    # Comments after nudge (excluding bot comments)
    comments_result = await session.execute(
        select(CommentModel).where(
            CommentModel.repo_id == repo_id,
            CommentModel.target_type == "pr",
            CommentModel.target_number == target_number,
            CommentModel.github_created_at > nudged_at,
            CommentModel.is_bot == False,
        )
    )
    comments = comments_result.scalars().all()
    if comments:
        observed["activity_found"].append(f"{len(comments)} human comment(s)")

    # Events on this PR after nudge
    events_result = await session.execute(
        select(EventModel).where(
            EventModel.repo_id == repo_id,
            EventModel.event_type.in_(["pull_request", "pull_request_review"]),
            EventModel.target_number == target_number,
            EventModel.received_at > nudged_at,
        )
    )
    events = events_result.scalars().all()
    if events:
        observed["activity_found"].append(f"{len(events)} PR event(s)")

    if observed["activity_found"]:
        return CheckerResult(
            status="success",
            observed=observed,
            notes=f"Nudge woke the PR: {', '.join(observed['activity_found'])}",
        )
    return CheckerResult(
        status="failed",
        observed=observed,
        notes="No activity after nudge",
    )


# ── Checker: deadline_prediction ────────────────────────────────────


async def check_deadline_prediction(
    repo_id: int,
    target_number: Optional[int],
    predicted: dict,
    session: AsyncSession,
) -> CheckerResult:
    """
    Was the deadline prediction accurate? Compare predicted close date
    against actual state of the milestone/issue.
    """
    if target_number is None:
        return CheckerResult(status="failed", notes="no target_number")

    expected_str = predicted.get("expected_close_by")
    if not expected_str:
        # No specific date — treat as informational check
        return CheckerResult(
            status="partial",
            notes="No expected_close_by in predicted — can't verify accuracy",
        )

    try:
        expected = datetime.fromisoformat(expected_str.replace("Z", "+00:00"))
        if expected.tzinfo is not None:
            expected = expected.replace(tzinfo=None)
    except (ValueError, TypeError):
        return CheckerResult(status="failed", notes=f"bad expected_close_by: {expected_str}")

    # Check the issue state
    issue = await _get_issue(session, repo_id, target_number)
    if not issue:
        return CheckerResult(status="failed", notes="target not in world model")

    now = datetime.utcnow()
    observed = {
        "expected_close_by": expected_str,
        "current_state": issue.state,
        "closed_at": issue.closed_at.isoformat() if issue.closed_at else None,
    }

    if issue.state == "closed" and issue.closed_at:
        diff = (issue.closed_at - expected).total_seconds()
        observed["days_off"] = round(diff / 86400, 1)
        if abs(diff) < 86400:  # within 1 day
            return CheckerResult(status="success", observed=observed,
                                 notes=f"Closed within 1 day of prediction")
        if diff < 86400 * 3:  # within 3 days late
            return CheckerResult(status="partial", observed=observed,
                                 notes=f"Closed {observed['days_off']} days after prediction")
        return CheckerResult(status="failed", observed=observed,
                             notes=f"Closed {observed['days_off']} days off prediction")

    # Still open
    if now > expected:
        days_over = round((now - expected).total_seconds() / 86400, 1)
        observed["days_over"] = days_over
        return CheckerResult(status="failed", observed=observed,
                             notes=f"Still open, {days_over} days past predicted close")

    return CheckerResult(status="partial", observed=observed,
                         notes="Still open, prediction window not yet reached")


# ── Registration ────────────────────────────────────────────────────


register_checker(OutcomeType.SMART_EVAL.value, check_smart_eval)
register_checker(OutcomeType.CLOSURE_VALIDATION.value, check_closure_validation)
register_checker(OutcomeType.CHECKLIST_CORRECTION.value, check_checklist_correction)
register_checker(OutcomeType.RISK_WARNING.value, check_risk_warning)
register_checker(OutcomeType.STALE_NUDGE.value, check_stale_nudge)
register_checker(OutcomeType.DEADLINE_PREDICTION.value, check_deadline_prediction)
