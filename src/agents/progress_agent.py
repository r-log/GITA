"""
Progress Tracker Agent — tracks milestone completion, velocity, and blockers.

Calculates velocity trends, predicts deadlines, identifies blocked issues,
and posts progress reports. Triggered on milestone events, push events,
and scheduled scans.
"""

from __future__ import annotations

import json
import structlog

from src.agents.base import BaseAgent, AgentContext, AgentResult
from src.tools.base import Tool

# GitHub tools
from src.tools.github.issues import make_get_issue, make_get_all_issues
from src.tools.github.milestones import make_get_milestone, make_get_all_milestones
from src.tools.github.pull_requests import make_get_open_prs
from src.tools.github.comments import make_post_comment, make_edit_comment
from src.tools.github.users import make_tag_user

# AI / computation tools
from src.tools.ai.predictor import (
    make_calculate_velocity,
    make_predict_completion,
    make_detect_blockers,
    make_detect_stale_prs,
)

# DB tools
from src.tools.db.analysis import make_save_analysis, make_get_analysis_history

log = structlog.get_logger()


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
        return [
            # GitHub
            make_get_issue(installation_id, repo_full_name),
            make_get_all_issues(installation_id, repo_full_name),
            make_get_milestone(installation_id, repo_full_name),
            make_get_all_milestones(installation_id, repo_full_name),
            make_get_open_prs(installation_id, repo_full_name),
            make_post_comment(installation_id, repo_full_name),
            make_edit_comment(installation_id, repo_full_name),
            make_tag_user(installation_id, repo_full_name),
            # AI / computation
            make_calculate_velocity(),
            make_predict_completion(),
            make_detect_blockers(),
            make_detect_stale_prs(),
            # DB
            make_save_analysis(repo_id),
            make_get_analysis_history(repo_id),
        ]

    async def handle(self, context: AgentContext) -> AgentResult:
        log.info(
            "progress_tracker_start",
            repo=self.repo_full_name,
            webhook_event=context.event_type,
        )

        # Determine what milestone to focus on from the event
        milestone_data = context.event_payload.get("milestone", {})
        issue_data = context.event_payload.get("issue", {})

        focus_info = {}
        if milestone_data:
            focus_info["milestone_number"] = milestone_data.get("number")
            focus_info["milestone_title"] = milestone_data.get("title")
        elif issue_data and issue_data.get("milestone"):
            m = issue_data["milestone"]
            focus_info["milestone_number"] = m.get("number")
            focus_info["milestone_title"] = m.get("title")

        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": json.dumps({
                    "task": "Track progress and report",
                    "event": context.event_type,
                    "repo": self.repo_full_name,
                    "focus": focus_info,
                    "instructions": (
                        f"A '{context.event_type}' event occurred in {self.repo_full_name}. "
                        + (f"Focus on milestone '{focus_info.get('milestone_title', 'unknown')}'. " if focus_info else "Check all open milestones. ")
                        + "Follow your instructions to calculate velocity, detect blockers, and report."
                    ),
                }),
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
