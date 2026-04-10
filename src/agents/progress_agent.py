"""
Progress Tracker Agent — tracks milestone completion, velocity, and blockers.

Architecture: gather-then-reason.
  1. Python gathers all data (issues, milestones, velocity, file coverage, blockers)
  2. LLM receives assembled context and reasons about progress
  3. LLM uses output tools (post_comment, edit_comment, tag_user) to report
"""

from __future__ import annotations

import json
import re
import structlog

from src.agents.base import BaseAgent, AgentContext, AgentResult
from src.core.config import settings
from src.tools.base import Tool

# GitHub tools — raw functions for data gathering
from src.tools.github.issues import _get_all_issues, _update_issue
from src.tools.github.pull_requests import _get_open_prs

# GitHub tools — output (LLM decides when to use these)
from src.tools.github.issues import make_get_issue, make_get_all_issues, make_update_issue
from src.tools.github.milestones import make_get_milestone, make_get_all_milestones
from src.tools.github.comments import make_post_comment, make_edit_comment, make_upsert_tracked_comment
from src.tools.github.users import make_tag_user

# Computation tools — raw functions for gathering
from src.tools.ai.predictor import _calculate_velocity, _detect_blockers, _detect_stale_prs

# AI tools — LLM-callable for reasoning
from src.tools.ai.predictor import make_predict_completion

# DB tools
from src.tools.db.analysis import make_save_analysis, make_get_analysis_history
from src.tools.db.rag_queries import make_search_events, make_search_commits
from src.tools.db.graph_queries import _get_milestone_file_coverage, _get_file_ownership
from src.tools.db.code_index import _query_code_index

log = structlog.get_logger()

# Pattern to extract issue numbers from checklist items
_CHECKLIST_RE = re.compile(r"- \[([ xX])\] .+?\(#(\d+)\)")


class ProgressTrackerAgent(BaseAgent):
    """
    Tracks milestone completion, calculates velocity, predicts deadlines,
    and identifies blockers.
    """

    def __init__(self, installation_id: int, repo_full_name: str, repo_id: int = 0, model: str | None = None):
        tools = self._build_tools(installation_id, repo_full_name, repo_id)

        super().__init__(
            name="progress_tracker",
            description="Progress analyst — tracks milestone completion %, velocity trends, blockers, and deadline predictions",
            tools=tools,
            model=model,
            system_prompt_file="progress_tracker.md",
        )

        self.installation_id = installation_id
        self.repo_full_name = repo_full_name
        self.repo_id = repo_id

    def _build_tools(self, installation_id: int, repo_full_name: str, repo_id: int) -> list[Tool]:
        """LLM only gets output and lookup tools — gathering is done in Python."""
        return [
            # Lookup (LLM may need to check specific issues/milestones)
            make_get_issue(installation_id, repo_full_name),
            make_get_all_issues(installation_id, repo_full_name),
            make_get_milestone(installation_id, repo_full_name),
            make_get_all_milestones(installation_id, repo_full_name),
            make_update_issue(installation_id, repo_full_name),
            # AI reasoning
            make_predict_completion(),
            # Output
            make_post_comment(installation_id, repo_full_name, repo_id),
            make_edit_comment(installation_id, repo_full_name),
            make_upsert_tracked_comment(installation_id, repo_full_name, repo_id),
            make_tag_user(installation_id, repo_full_name),
            # DB
            make_save_analysis(repo_id),
            make_get_analysis_history(repo_id),
            # RAG
            make_search_events(repo_id),
            make_search_commits(repo_id),
        ]

    async def _gather_context(self, focus_milestone_number: int | None = None) -> dict:
        """
        Gather all progress data in Python before the LLM call.
        This replaces 6-8 tool calls the LLM used to make.
        """
        gathered: dict = {"trackers": [], "open_prs": [], "velocity": {}, "blockers": {}}

        # 1. Get all issues
        all_issues_result = await _get_all_issues(
            self.installation_id, self.repo_full_name, state="all",
        )
        all_issues = all_issues_result.data if all_issues_result.success else []

        # 2. Find Milestone Tracker issues
        trackers = [
            i for i in all_issues
            if any(
                (l.get("name") if isinstance(l, dict) else l) == "Milestone Tracker"
                for l in i.get("labels", [])
            )
            and i.get("state") == "open"
        ]

        # If focusing on a specific milestone, filter
        if focus_milestone_number:
            trackers = [t for t in trackers if t.get("number") == focus_milestone_number] or trackers

        # 3. For each tracker, parse checklist and gather sub-issue states
        for tracker in trackers:
            body = tracker.get("body", "") or ""
            checklist_matches = _CHECKLIST_RE.findall(body)
            sub_issues = []

            for check_mark, issue_num in checklist_matches:
                num = int(issue_num)
                # Find in all_issues (avoid extra API calls)
                sub = next((i for i in all_issues if i.get("number") == num), None)
                if sub:
                    sub_issues.append({
                        "number": num,
                        "title": sub.get("title", ""),
                        "state": sub.get("state", "open"),
                        "checked": check_mark.lower() == "x",
                        "assignees": [a.get("login") for a in sub.get("assignees", []) if isinstance(a, dict)],
                        "updated_at": sub.get("updated_at", ""),
                    })

            # Calculate velocity for this tracker's issues
            velocity_result = await _calculate_velocity(sub_issues)
            velocity_data = velocity_result.data if velocity_result.success else {}

            # Detect blockers
            blockers_result = await _detect_blockers(sub_issues)
            blockers_data = blockers_result.data if blockers_result.success else {}

            # Get milestone file coverage (graph)
            file_coverage = {}
            # Use tracker's number as a rough milestone ID lookup — we need the DB milestone_id
            # For now, pass repo_id context
            try:
                coverage_result = await _get_milestone_file_coverage(self.repo_id, tracker.get("number", 0))
                file_coverage = coverage_result.data if coverage_result.success else {}
            except Exception:
                pass

            gathered["trackers"].append({
                "number": tracker.get("number"),
                "title": tracker.get("title", ""),
                "sub_issues": sub_issues,
                "total_tasks": len(sub_issues),
                "completed_tasks": sum(1 for s in sub_issues if s["state"] == "closed"),
                "velocity": velocity_data,
                "blockers": blockers_data,
                "file_coverage": file_coverage,
            })

        # 4. Get open PRs
        prs_result = await _get_open_prs(self.installation_id, self.repo_full_name)
        if prs_result.success:
            # Detect stale PRs
            stale_result = await _detect_stale_prs(prs_result.data or [])
            gathered["open_prs"] = {
                "count": len(prs_result.data or []),
                "stale": stale_result.data if stale_result.success else {},
            }

        return gathered

    async def _gather_push_context(self, payload: dict) -> dict:
        """
        Extract push-specific context: which files changed, which issues/milestones
        they belong to, and what the commit messages say.

        This connects a push to the project structure via the knowledge graph.
        """
        push_context: dict = {"changed_files": [], "affected_issues": {}, "commits": []}

        # Extract changed files and commit info from the push payload
        commits = payload.get("commits", [])
        changed_files = set()
        for commit in commits:
            changed_files.update(commit.get("added", []))
            changed_files.update(commit.get("modified", []))
            push_context["commits"].append({
                "sha": commit.get("id", "")[:10],
                "message": commit.get("message", "")[:200],
                "author": (commit.get("author") or {}).get("username") or (commit.get("author") or {}).get("name", "unknown"),
                "files_touched": len(commit.get("added", []) + commit.get("modified", []) + commit.get("removed", [])),
            })

        push_context["changed_files"] = sorted(changed_files)

        if not changed_files:
            return push_context

        # Query the knowledge graph: which issues/milestones own these files?
        try:
            ownership_result = await _get_file_ownership(self.repo_id, list(changed_files))
            if ownership_result.success and ownership_result.data.get("files"):
                for file_entry in ownership_result.data["files"]:
                    path = file_entry.get("file_path", "")
                    for entity in file_entry.get("entities", []):
                        entity_type = entity.get("type", "")
                        entity_id = entity.get("id")
                        key = f"{entity_type}:{entity_id}"
                        if key not in push_context["affected_issues"]:
                            push_context["affected_issues"][key] = {
                                "type": entity_type,
                                "id": entity_id,
                                "files": [],
                            }
                        push_context["affected_issues"][key]["files"].append(path)

                # Convert to list for JSON serialization
                push_context["affected_issues"] = list(push_context["affected_issues"].values())
        except Exception as e:
            log.warning("push_ownership_lookup_failed", error=str(e))
            push_context["affected_issues"] = []

        # Also scan commit messages for issue references (#42, fixes #42, etc.)
        issue_ref_pattern = re.compile(r'(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#(\d+)', re.IGNORECASE)
        mentioned_pattern = re.compile(r'#(\d+)')
        referenced_issues: set[int] = set()
        resolved_issues: set[int] = set()

        for commit in commits:
            msg = commit.get("message", "")
            for match in issue_ref_pattern.finditer(msg):
                resolved_issues.add(int(match.group(1)))
            for match in mentioned_pattern.finditer(msg):
                referenced_issues.add(int(match.group(1)))

        push_context["issues_referenced_in_commits"] = sorted(referenced_issues)
        push_context["issues_resolved_in_commits"] = sorted(resolved_issues)

        # Query the code_index for the STRUCTURE of changed files (already in DB from reindex)
        # This tells the LLM what functions/classes/routes were added or modified
        code_structures = []
        for file_path in list(changed_files)[:15]:  # cap to avoid huge briefs
            try:
                idx_result = await _query_code_index(self.repo_id, file_path=file_path)
                if idx_result.success and idx_result.data:
                    for record in idx_result.data:
                        structure = record.get("structure", {})
                        # Only include the meaningful parts
                        code_structures.append({
                            "file": record["file_path"],
                            "language": record["language"],
                            "lines": record["line_count"],
                            "functions": [f.get("name") for f in structure.get("functions", [])],
                            "classes": [c.get("name") for c in structure.get("classes", [])],
                            "routes": [f"{r.get('method', '')} {r.get('path', '')}" for r in structure.get("routes", [])],
                            "imports": structure.get("imports", []),
                        })
            except Exception:
                pass

        push_context["code_structures"] = code_structures

        return push_context

    async def handle(self, context: AgentContext) -> AgentResult:
        log.info(
            "progress_tracker_start",
            repo=self.repo_full_name,
            webhook_event=context.event_type,
        )

        # Determine focus milestone from event
        milestone_data = context.event_payload.get("milestone", {})
        issue_data = context.event_payload.get("issue", {})

        focus_number = None
        focus_title = None
        if milestone_data:
            focus_number = milestone_data.get("number")
            focus_title = milestone_data.get("title")
        elif issue_data and issue_data.get("milestone"):
            m = issue_data["milestone"]
            focus_number = m.get("number")
            focus_title = m.get("title")

        # Phase 1: Gather all data in Python
        log.info("progress_tracker_gathering", focus=focus_title)
        gathered = await self._gather_context(focus_number)

        # Phase 1b: For push events, also gather push-specific context
        push_context = None
        if context.event_type == "push":
            push_context = await self._gather_push_context(context.event_payload)
            log.info(
                "progress_tracker_push_context",
                changed_files=len(push_context.get("changed_files", [])),
                affected_issues=len(push_context.get("affected_issues", [])),
                resolved_in_commits=push_context.get("issues_resolved_in_commits", []),
            )

        # Phase 1c: Deterministic auto-close for 100% complete trackers
        # This runs BEFORE the LLM — no judgment needed, just math.
        # Uses a Redis lock to prevent duplicate closes from parallel events.
        auto_closed_trackers = []
        for tracker in gathered.get("trackers", []):
            total = tracker.get("total_tasks", 0)
            completed = tracker.get("completed_tasks", 0)
            tracker_num = tracker.get("number", 0)

            if total > 0 and completed == total and tracker_num:
                # Dedup: Redis lock prevents two parallel agents from closing the same tracker
                try:
                    import redis.asyncio as aioredis
                    r = aioredis.from_url(settings.redis_url)
                    lock_key = f"tracker_close:{self.repo_full_name}:{tracker_num}"
                    acquired = await r.set(lock_key, "1", ex=60, nx=True)
                    if not acquired:
                        log.info("tracker_close_dedup", tracker=tracker_num)
                        continue
                except Exception:
                    pass  # If Redis fails, proceed anyway

                close_result = await _update_issue(
                    self.installation_id, self.repo_full_name,
                    tracker_num, state="closed",
                )
                if close_result.success:
                    auto_closed_trackers.append(tracker_num)
                    log.info(
                        "tracker_auto_closed",
                        tracker=tracker_num,
                        title=tracker.get("title"),
                        completed=completed,
                        total=total,
                    )

        # Phase 2: Send everything to LLM for reasoning + output decisions
        push_section = ""
        if push_context:
            push_section = (
                "\n\n## PUSH CONTEXT\n"
                "This event is a code push. The world model already contains the parsed "
                "structure of every changed file (from the code index, not raw diffs).\n\n"
                f"**Files changed:** {push_context['changed_files']}\n"
                f"**Commits:** {push_context['commits']}\n"
                f"**Code structures (from DB):** {push_context.get('code_structures', [])}\n"
                f"**Issues/milestones owning these files (knowledge graph):** {push_context['affected_issues']}\n"
                f"**Issues referenced in commit messages:** {push_context['issues_referenced_in_commits']}\n"
                f"**Issues explicitly resolved (fixes/closes in commits):** {push_context['issues_resolved_in_commits']}\n"
                "\n## YOUR JOB ON PUSH EVENTS\n"
                "1. Look at the code structures — what was ADDED or CHANGED (new functions, classes, routes)?\n"
                "2. Cross-reference with the affected issues — does this code solve what those issues describe?\n"
                "3. If commit messages say 'fixes #N' or 'closes #N', that's strong evidence.\n"
                "4. If file ownership links files to issues AND the code structures show relevant implementations, that's evidence too.\n"
                "5. For each issue you're confident is resolved: use update_issue to close it, "
                "AND post a comment explaining WHAT code resolved it and WHY.\n"
                "6. Update any Milestone Tracker checklists accordingly.\n"
                "7. If you're NOT confident an issue is resolved, do NOT close it — just note the progress.\n"
                "\nBe decisive but accurate. Close what's clearly done. Leave open what's uncertain."
            )

        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": json.dumps({
                    "task": "Analyze milestone progress and report if off-track",
                    "repo": self.repo_full_name,
                    "event": context.event_type,
                    "focus": {"milestone_number": focus_number, "milestone_title": focus_title} if focus_number else None,
                    "gathered_data": {
                        "trackers": gathered["trackers"],
                        "open_prs": gathered["open_prs"],
                    },
                    "push_context": push_context if push_context else None,
                    "instructions": (
                        "All milestone data has been gathered above. For each tracker, you have: "
                        "sub-issue states, velocity metrics, blocker detection, and file coverage. "
                        "Decide if any milestones are off-track. If a sub-issue was recently closed, update "
                        "the tracker checklist. Use predict_completion if you need deadline estimates.\n\n"
                        "IMPORTANT: For progress reports, use upsert_progress_comment (NOT post_comment). "
                        "This edits the existing progress comment in place instead of creating a new one. "
                        "Each Milestone Tracker should have exactly ONE progress comment that gets "
                        "updated over time. Include: completion %, tasks done/total, velocity, "
                        "blockers, and remaining work."
                        + push_section
                    ),
                }, default=str),
            },
        ]

        final_text, tool_call_log = await self.run_tool_loop(messages)

        log.info(
            "progress_tracker_complete",
            repo=self.repo_full_name,
            tool_calls=len(tool_call_log),
        )

        return AgentResult(
            agent_name=self.name,
            status="success",
            actions_taken=[
                {"tool": tc["tool"], "success": tc["result"]["success"]}
                for tc in tool_call_log
            ],
            data={"final_response": final_text, "tool_call_log": tool_call_log},
            confidence=0.8,
            should_notify=any(tc["tool"] in ("post_comment", "tag_user") for tc in tool_call_log),
        )
