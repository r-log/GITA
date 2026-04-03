"""
Reconciliation system — compares stored project plan with current GitHub issue states.

Closes done issues that are still open, updates Milestone Tracker checklists,
and flags drift. Triggered by ARQ cron job or manual API call.
"""

import re
from datetime import datetime

import structlog
from sqlalchemy import select
from thefuzz import fuzz

from src.core.database import async_session
from src.models.onboarding_run import OnboardingRun
from src.models.repository import Repository
from src.tools.github.issues import _get_all_issues, _update_issue
from src.tools.db.onboarding import _save_onboarding_run

log = structlog.get_logger()

# Fuzzy match threshold for matching plan tasks to GitHub issues
MATCH_THRESHOLD = 70


async def _load_latest_run(repo_id: int) -> OnboardingRun | None:
    """Load the latest successful/context_update onboarding run."""
    async with async_session() as session:
        stmt = (
            select(OnboardingRun)
            .where(
                OnboardingRun.repo_id == repo_id,
                OnboardingRun.status.in_(["success", "partial", "context_update"]),
            )
            .order_by(OnboardingRun.completed_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


def _match_task_to_issue(task_title: str, issues: list[dict]) -> dict | None:
    """Find the best matching GitHub issue for a plan task using fuzzy matching."""
    best_score = 0
    best_match = None
    for issue in issues:
        score = fuzz.ratio(task_title.lower(), issue.get("title", "").lower())
        if score > best_score:
            best_score = score
            best_match = issue
    if best_score >= MATCH_THRESHOLD:
        return best_match
    return None


def _parse_checklist(body: str) -> list[dict]:
    """Parse markdown checklist items from a Milestone Tracker body."""
    pattern = re.compile(r"- \[([ xX])\] (.+?)(?:\(#(\d+)\))?$", re.MULTILINE)
    items = []
    for match in pattern.finditer(body):
        items.append({
            "checked": match.group(1).lower() == "x",
            "text": match.group(2).strip(),
            "issue_number": int(match.group(3)) if match.group(3) else None,
            "full_match": match.group(0),
        })
    return items


def _update_checklist(body: str, issue_states: dict[int, str]) -> str | None:
    """
    Update checklist marks based on current issue states.
    Returns updated body or None if no changes needed.
    """
    updated = body
    changed = False

    for number, state in issue_states.items():
        should_be_checked = state == "closed"

        # Match both checked and unchecked patterns for this issue number
        pattern = re.compile(rf"- \[([ xX])\] (.+?)\(#{number}\)")
        match = pattern.search(updated)
        if not match:
            continue

        currently_checked = match.group(1).lower() == "x"
        if should_be_checked and not currently_checked:
            updated = updated[:match.start()] + updated[match.start():match.end()].replace("[ ]", "[x]") + updated[match.end():]
            changed = True
        elif not should_be_checked and currently_checked:
            updated = updated[:match.start()] + updated[match.start():match.end()].replace("[x]", "[ ]").replace("[X]", "[ ]") + updated[match.end():]
            changed = True

    return updated if changed else None


async def reconcile_repo(
    repo_id: int,
    repo_full_name: str,
    installation_id: int,
) -> dict:
    """
    Reconcile stored plan with current GitHub issue states.
    Closes done issues, updates checklists, flags drift.
    """
    log.info("reconcile_start", repo=repo_full_name)

    # 1. Load latest onboarding run
    latest_run = await _load_latest_run(repo_id)
    if not latest_run:
        log.info("reconcile_skip", repo=repo_full_name, reason="no_onboarding_run")
        return {"status": "skipped", "reason": "no_onboarding_run"}

    plan = latest_run.suggested_plan or {}
    milestones = plan.get("milestones", [])
    if not milestones:
        log.info("reconcile_skip", repo=repo_full_name, reason="no_milestones_in_plan")
        return {"status": "skipped", "reason": "no_milestones_in_plan"}

    # 2. Fetch current issues from GitHub
    issues_result = await _get_all_issues(installation_id, repo_full_name, state="all")
    if not issues_result.success:
        log.error("reconcile_fetch_failed", error=issues_result.error)
        return {"status": "failed", "error": f"Failed to fetch issues: {issues_result.error}"}

    all_issues = issues_result.data
    open_issues = [i for i in all_issues if i.get("state") == "open"]
    closed_issues = [i for i in all_issues if i.get("state") == "closed"]
    issue_by_number = {i["number"]: i for i in all_issues}

    log.info("reconcile_issues_fetched", total=len(all_issues), open=len(open_issues), closed=len(closed_issues))

    # 3. Compare plan tasks with issue states
    actions_log = []
    issues_closed = 0
    issues_updated = 0
    drift_flags = []

    for milestone in milestones:
        for task in milestone.get("tasks", []):
            task_title = task.get("title", "")
            task_status = task.get("status", "")

            # Find matching GitHub issue
            matched_issue = _match_task_to_issue(task_title, all_issues)
            if not matched_issue:
                continue

            issue_number = matched_issue["number"]
            issue_state = matched_issue.get("state", "open")

            # Close done issues that are still open
            if task_status in ("done", "complete") and issue_state == "open":
                result = await _update_issue(
                    installation_id, repo_full_name, issue_number, state="closed"
                )
                if result.success:
                    issues_closed += 1
                    # Update local state so checklist step sees correct states
                    issue_by_number[issue_number]["state"] = "closed"
                    actions_log.append({
                        "action": "close",
                        "issue_number": issue_number,
                        "title": matched_issue["title"],
                        "reason": f"Task marked as '{task_status}' in plan",
                    })
                    log.info("reconcile_closed", issue=issue_number, title=matched_issue["title"])

            # Flag drift: issue closed on GitHub but plan says in-progress/not-started
            elif task_status in ("not-started", "in-progress") and issue_state == "closed":
                drift_flags.append({
                    "type": "unexpected_closure",
                    "issue_number": issue_number,
                    "title": matched_issue["title"],
                    "plan_status": task_status,
                })

    # 4. Update Milestone Tracker checklists
    milestone_trackers = [
        i for i in all_issues
        if any(l.get("name") == "Milestone Tracker" for l in i.get("labels", []))
    ]

    for tracker in milestone_trackers:
        body = tracker.get("body", "")
        if not body:
            continue

        checklist = _parse_checklist(body)
        if not checklist:
            continue

        # Build state map for linked issues
        linked_states = {}
        for item in checklist:
            if item["issue_number"] and item["issue_number"] in issue_by_number:
                linked_states[item["issue_number"]] = issue_by_number[item["issue_number"]]["state"]

        # Update checklist marks
        updated_body = _update_checklist(body, linked_states)
        if updated_body:
            result = await _update_issue(
                installation_id, repo_full_name, tracker["number"], body=updated_body
            )
            if result.success:
                issues_updated += 1
                actions_log.append({
                    "action": "update_checklist",
                    "issue_number": tracker["number"],
                    "title": tracker["title"],
                })
                log.info("reconcile_checklist_updated", issue=tracker["number"], title=tracker["title"])

        # Auto-close Milestone Tracker if ALL linked sub-issues are closed
        if linked_states and tracker.get("state") == "open":
            all_closed = all(s == "closed" for s in linked_states.values())
            if all_closed:
                result = await _update_issue(
                    installation_id, repo_full_name, tracker["number"], state="closed"
                )
                if result.success:
                    issues_closed += 1
                    issue_by_number[tracker["number"]]["state"] = "closed"
                    actions_log.append({
                        "action": "close_milestone",
                        "issue_number": tracker["number"],
                        "title": tracker["title"],
                        "reason": "All sub-issues are closed",
                    })
                    log.info("reconcile_milestone_closed", issue=tracker["number"], title=tracker["title"])

    # 5. Save reconciliation record
    await _save_onboarding_run(
        repo_id=repo_id,
        status="reconciliation",
        repo_snapshot=latest_run.repo_snapshot or {},
        suggested_plan=latest_run.suggested_plan or {},
        existing_state={"current_issues_snapshot": [
            {"number": i["number"], "title": i["title"], "state": i["state"]}
            for i in all_issues
        ]},
        actions_taken=actions_log,
        issues_updated=issues_closed + issues_updated,
        confidence=0.95,
    )

    summary = {
        "status": "success",
        "issues_closed": issues_closed,
        "checklists_updated": issues_updated,
        "drift_flags": len(drift_flags),
        "actions": actions_log,
    }

    log.info(
        "reconcile_complete",
        repo=repo_full_name,
        issues_closed=issues_closed,
        checklists_updated=issues_updated,
        drift_flags=len(drift_flags),
    )

    return summary


async def reconcile_all_repos() -> list[dict]:
    """Reconcile all tracked repos. Called by cron and manual API."""
    log.info("reconcile_all_start")

    async with async_session() as session:
        result = await session.execute(select(Repository))
        repos = result.scalars().all()

    results = []
    for repo in repos:
        try:
            r = await reconcile_repo(repo.id, repo.full_name, repo.installation_id)
            results.append({"repo": repo.full_name, **r})
        except Exception as e:
            log.error("reconcile_repo_error", repo=repo.full_name, error=str(e))
            results.append({"repo": repo.full_name, "status": "failed", "error": str(e)})

    log.info("reconcile_all_complete", repos=len(results))
    return results


async def reconcile_single_repo(repo_full_name: str) -> dict:
    """Reconcile a single repo by name."""
    async with async_session() as session:
        result = await session.execute(
            select(Repository).where(Repository.full_name == repo_full_name)
        )
        repo = result.scalar_one_or_none()

    if not repo:
        raise ValueError(f"Repository not found: {repo_full_name}")

    return await reconcile_repo(repo.id, repo.full_name, repo.installation_id)
