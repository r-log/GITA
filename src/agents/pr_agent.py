"""
PR Review Agent — analyzes pull requests for quality and milestone alignment.

Checks diff quality, test coverage, linked issues, and creates check runs.
Triggered on pull_request.opened and pull_request.synchronize events.
"""

from __future__ import annotations

import json
import structlog

from src.agents.base import BaseAgent, AgentContext, AgentResult
from src.tools.base import Tool

# GitHub tools
from src.tools.github.pull_requests import make_get_pr, make_get_pr_diff, make_get_pr_files, make_get_open_prs
from src.tools.github.issues import make_get_issue, make_get_all_issues
from src.tools.github.milestones import make_get_milestone
from src.tools.github.comments import make_post_comment
from src.tools.github.checks import make_create_check_run
from src.tools.github.users import make_tag_user

# AI tools
from src.tools.ai.code_analyzer import make_analyze_diff_quality, make_check_test_coverage
from src.tools.ai.smart_evaluator import make_check_milestone_alignment

# DB tools
from src.tools.db.analysis import make_save_analysis

log = structlog.get_logger()


class PRReviewAgent(BaseAgent):
    """
    Analyzes pull requests for code quality, test coverage,
    linked issue verification, and milestone alignment.
    """

    def __init__(self, installation_id: int, repo_full_name: str, repo_id: int = 0, model: str | None = None):
        tools = self._build_tools(installation_id, repo_full_name, repo_id)

        super().__init__(
            name="pr_reviewer",
            description="PR reviewer — analyzes diffs for quality, checks test coverage, verifies linked issues and milestone alignment",
            tools=tools,
            model=model,
            system_prompt_file="pr_reviewer.md",
        )

        self.installation_id = installation_id
        self.repo_full_name = repo_full_name
        self.repo_id = repo_id

    def _build_tools(self, installation_id: int, repo_full_name: str, repo_id: int) -> list[Tool]:
        return [
            # GitHub — PR
            make_get_pr(installation_id, repo_full_name),
            make_get_pr_diff(installation_id, repo_full_name),
            make_get_pr_files(installation_id, repo_full_name),
            make_get_open_prs(installation_id, repo_full_name),
            # GitHub — issues & milestones
            make_get_issue(installation_id, repo_full_name),
            make_get_all_issues(installation_id, repo_full_name),
            make_get_milestone(installation_id, repo_full_name),
            # GitHub — output
            make_post_comment(installation_id, repo_full_name),
            make_create_check_run(installation_id, repo_full_name),
            make_tag_user(installation_id, repo_full_name),
            # AI
            make_analyze_diff_quality(),
            make_check_test_coverage(),
            make_check_milestone_alignment(),
            # DB
            make_save_analysis(repo_id),
        ]

    async def handle(self, context: AgentContext) -> AgentResult:
        log.info(
            "pr_reviewer_start",
            repo=self.repo_full_name,
            webhook_event=context.event_type,
        )

        pr_data = context.event_payload.get("pull_request", {})
        pr_number = pr_data.get("number", 0)

        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": json.dumps({
                    "task": "Review this pull request",
                    "event": context.event_type,
                    "repo": self.repo_full_name,
                    "pr_number": pr_number,
                    "pr_summary": {
                        "title": pr_data.get("title"),
                        "body": (pr_data.get("body") or "")[:1000],
                        "author": pr_data.get("user", {}).get("login"),
                        "base": pr_data.get("base", {}).get("ref"),
                        "head": pr_data.get("head", {}).get("ref"),
                        "head_sha": pr_data.get("head", {}).get("sha"),
                        "additions": pr_data.get("additions"),
                        "deletions": pr_data.get("deletions"),
                        "changed_files": pr_data.get("changed_files"),
                    },
                    "instructions": (
                        f"A '{context.event_type}' event occurred on PR #{pr_number} in {self.repo_full_name}. "
                        "Follow your instructions to review the diff, check quality and tests, "
                        "verify linked issues, and create a check run."
                    ),
                }),
            },
        ]

        final_text, tool_call_log = await self.run_tool_loop(messages)

        log.info(
            "pr_reviewer_complete",
            repo=self.repo_full_name,
            pr=pr_number,
            tool_calls=len(tool_call_log),
        )

        return AgentResult(
            agent_name=self.name,
            status="success",
            actions_taken=[
                {"tool": tc["tool"], "success": tc["result"]["success"]}
                for tc in tool_call_log
            ],
            data={"final_response": final_text, "tool_call_log": tool_call_log, "pr_number": pr_number},
            confidence=0.8,
            should_notify=any(tc["tool"] in ("post_comment", "create_check_run") for tc in tool_call_log),
        )
