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
from src.tools.base import Tool

# GitHub tools — raw functions for data gathering
from src.tools.github.issues import _get_all_issues
from src.tools.github.pull_requests import _get_open_prs

# GitHub tools — output (LLM decides when to use these)
from src.tools.github.issues import make_get_issue, make_get_all_issues, make_update_issue
from src.tools.github.milestones import make_get_milestone, make_get_all_milestones
from src.tools.github.comments import make_post_comment, make_edit_comment
from src.tools.github.users import make_tag_user

# Computation tools — raw functions for gathering
from src.tools.ai.predictor import _calculate_velocity, _detect_blockers, _detect_stale_prs

# AI tools — LLM-callable for reasoning
from src.tools.ai.predictor import make_predict_completion

# DB tools
from src.tools.db.analysis import make_save_analysis, make_get_analysis_history
from src.tools.db.graph_queries import _get_milestone_file_coverage

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
            make_post_comment(installation_id, repo_full_name),
            make_edit_comment(installation_id, repo_full_name),
            make_tag_user(installation_id, repo_full_name),
            # DB
            make_save_analysis(repo_id),
            make_get_analysis_history(repo_id),
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

        # Phase 2: Send everything to LLM for reasoning + output decisions
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
                    "instructions": (
                        "All milestone data has been gathered above. For each tracker, you have: "
                        "sub-issue states, velocity metrics, blocker detection, and file coverage. "
                        "Decide if any milestones are off-track. If so, post ONE progress report "
                        "comment on the tracker issue. If a sub-issue was recently closed, update "
                        "the tracker checklist. Use predict_completion if you need deadline estimates."
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
