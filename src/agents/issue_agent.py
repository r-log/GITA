"""
Issue Analyst Agent — evaluates issues against S.M.A.R.T. criteria.

Checks milestone alignment, detects poorly defined issues, and suggests
improvements. Triggered on issues.opened, issues.edited, issues.milestoned,
issues.assigned events.
"""

from __future__ import annotations

import json
import structlog

from src.agents.base import BaseAgent, AgentContext, AgentResult
from src.tools.base import Tool

# GitHub tools
from src.tools.github.issues import make_get_issue, make_get_all_issues
from src.tools.github.milestones import make_get_milestone, make_get_all_milestones
from src.tools.github.comments import make_post_comment, make_edit_comment
from src.tools.github.labels import make_add_label
from src.tools.github.users import make_tag_user

# AI tools
from src.tools.ai.smart_evaluator import make_evaluate_smart, make_check_milestone_alignment

# DB tools
from src.tools.db.analysis import make_save_evaluation, make_get_previous_evaluation, make_save_analysis

log = structlog.get_logger()


class IssueAnalystAgent(BaseAgent):
    """
    Evaluates issues against S.M.A.R.T. criteria, checks milestone alignment,
    and posts constructive feedback.
    """

    def __init__(self, installation_id: int, repo_full_name: str, repo_id: int = 0):
        tools = self._build_tools(installation_id, repo_full_name, repo_id)

        super().__init__(
            name="issue_analyst",
            description="Issue quality analyst — evaluates issues with S.M.A.R.T. criteria, checks milestone alignment, suggests improvements",
            tools=tools,
            system_prompt_file="issue_analyst.md",
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
            make_post_comment(installation_id, repo_full_name),
            make_edit_comment(installation_id, repo_full_name),
            make_add_label(installation_id, repo_full_name),
            make_tag_user(installation_id, repo_full_name),
            # AI
            make_evaluate_smart(),
            make_check_milestone_alignment(),
            # DB — use 0 as placeholder issue_db_id; agent looks up the right one
            make_save_evaluation(0),
            make_get_previous_evaluation(0),
            make_save_analysis(repo_id),
        ]

    async def handle(self, context: AgentContext) -> AgentResult:
        log.info(
            "issue_analyst_start",
            repo=self.repo_full_name,
            event=context.event_type,
        )

        # Extract issue number from the webhook payload
        issue_data = context.event_payload.get("issue", {})
        issue_number = issue_data.get("number", 0)

        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": json.dumps({
                    "task": "Analyze this issue",
                    "event": context.event_type,
                    "repo": self.repo_full_name,
                    "issue_number": issue_number,
                    "issue_summary": {
                        "title": issue_data.get("title"),
                        "state": issue_data.get("state"),
                        "labels": [l.get("name") if isinstance(l, dict) else l for l in issue_data.get("labels", [])],
                        "assignees": [a.get("login") if isinstance(a, dict) else a for a in issue_data.get("assignees", [])],
                        "milestone": issue_data.get("milestone", {}).get("title") if issue_data.get("milestone") else None,
                    },
                    "instructions": (
                        f"A '{context.event_type}' event occurred on issue #{issue_number} in {self.repo_full_name}. "
                        "Follow your instructions to evaluate the issue and take appropriate action."
                    ),
                }),
            },
        ]

        final_text, tool_call_log = await self.run_tool_loop(messages)

        log.info(
            "issue_analyst_complete",
            repo=self.repo_full_name,
            issue=issue_number,
            tool_calls=len(tool_call_log),
        )

        return AgentResult(
            agent_name=self.name,
            status="success",
            actions_taken=[
                {"tool": tc["tool"], "success": tc["result"]["success"]}
                for tc in tool_call_log
            ],
            data={"final_response": final_text, "tool_call_log": tool_call_log, "issue_number": issue_number},
            confidence=0.8,
            should_notify=any(tc["tool"] == "post_comment" for tc in tool_call_log),
        )
