"""
Risk Detective Agent — scans for security risks, breaking changes, and dependency issues.

Runs in parallel with the PR Review Agent on pull_request events.
Triggered on pull_request.opened, pull_request.synchronize, and push events.
"""

from __future__ import annotations

import json
import structlog

from src.agents.base import BaseAgent, AgentContext, AgentResult
from src.tools.base import Tool

# GitHub tools
from src.tools.github.pull_requests import make_get_pr, make_get_pr_diff, make_get_pr_files, make_get_open_prs
from src.tools.github.repos import make_read_file
from src.tools.github.comments import make_post_comment
from src.tools.github.checks import make_create_check_run
from src.tools.github.users import make_tag_user

# AI tools
from src.tools.ai.risk_scanner import (
    make_scan_secrets,
    make_scan_security_patterns,
    make_detect_breaking_changes,
    make_check_dependency_changes,
)

# DB tools
from src.tools.db.analysis import make_save_analysis

log = structlog.get_logger()


class RiskDetectiveAgent(BaseAgent):
    """
    Scans code changes for security risks, breaking changes,
    dependency issues, and potential merge conflicts.
    """

    def __init__(self, installation_id: int, repo_full_name: str, repo_id: int = 0, model: str | None = None):
        tools = self._build_tools(installation_id, repo_full_name, repo_id)

        super().__init__(
            name="risk_detective",
            description="Security and risk analyst — scans for secrets, vulnerabilities, breaking changes, and dependency risks",
            tools=tools,
            model=model,
            system_prompt_file="risk_detective.md",
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
            # GitHub — repo
            make_read_file(installation_id, repo_full_name),
            # GitHub — output
            make_post_comment(installation_id, repo_full_name),
            make_create_check_run(installation_id, repo_full_name),
            make_tag_user(installation_id, repo_full_name),
            # AI — scanning
            make_scan_secrets(),
            make_scan_security_patterns(),
            make_detect_breaking_changes(),
            make_check_dependency_changes(),
            # DB
            make_save_analysis(repo_id),
        ]

    async def handle(self, context: AgentContext) -> AgentResult:
        log.info(
            "risk_detective_start",
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
                    "task": "Scan this PR for risks",
                    "event": context.event_type,
                    "repo": self.repo_full_name,
                    "pr_number": pr_number,
                    "pr_summary": {
                        "title": pr_data.get("title"),
                        "author": pr_data.get("user", {}).get("login"),
                        "head_sha": pr_data.get("head", {}).get("sha"),
                        "additions": pr_data.get("additions"),
                        "deletions": pr_data.get("deletions"),
                        "changed_files": pr_data.get("changed_files"),
                    },
                    "instructions": (
                        f"A '{context.event_type}' event occurred on PR #{pr_number} in {self.repo_full_name}. "
                        "Follow your instructions to scan for secrets, security vulnerabilities, "
                        "breaking changes, and dependency risks. Create a check run with your findings."
                    ),
                }),
            },
        ]

        final_text, tool_call_log = await self.run_tool_loop(messages)

        log.info(
            "risk_detective_complete",
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
            confidence=0.85,
            should_notify=any(tc["tool"] in ("post_comment", "create_check_run", "tag_user") for tc in tool_call_log),
        )
