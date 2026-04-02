"""
Onboarding Agent — first-run setup when the app is installed on a new repo.

Scans the codebase, understands the project, creates/reconciles milestones
and issues. Works in 6 phases: scan → analyze → fetch → reconcile → execute → persist.
"""

from __future__ import annotations

import json
import structlog

from src.agents.base import BaseAgent, AgentContext, AgentResult
from src.tools.base import Tool

# GitHub tools
from src.tools.github.repos import make_get_repo_tree, make_read_file, make_get_collaborators
from src.tools.github.issues import make_get_issue, make_get_all_issues, make_create_issue, make_update_issue
from src.tools.github.milestones import (
    make_get_all_milestones,
    make_get_milestone,
    make_create_milestone,
    make_update_milestone,
)
from src.tools.github.labels import make_add_label, make_create_label
from src.tools.github.comments import make_post_comment

# AI tools
from src.tools.ai.project_planner import (
    make_infer_project_plan,
    make_compare_plan_vs_state,
    make_fuzzy_match_milestone,
)

# DB tools
from src.tools.db.onboarding import make_save_onboarding_run, make_save_file_mapping

log = structlog.get_logger()


class OnboardingAgent(BaseAgent):
    """
    First-run setup agent. Scans a repository, infers a project plan,
    reconciles with existing milestones/issues, and executes changes.
    """

    def __init__(self, installation_id: int, repo_full_name: str, repo_id: int = 0):
        # Build the scoped toolset for this installation/repo
        tools = self._build_tools(installation_id, repo_full_name, repo_id)

        super().__init__(
            name="onboarding",
            description="Project setup specialist — scans repos, creates milestones and issues, reconciles existing state",
            tools=tools,
            system_prompt_file="onboarding.md",
        )

        self.installation_id = installation_id
        self.repo_full_name = repo_full_name
        self.repo_id = repo_id

    def _build_tools(self, installation_id: int, repo_full_name: str, repo_id: int) -> list[Tool]:
        """Build the scoped tool list for this agent."""
        return [
            # GitHub — repo scanning
            make_get_repo_tree(installation_id, repo_full_name),
            make_read_file(installation_id, repo_full_name),
            make_get_collaborators(installation_id, repo_full_name),
            # GitHub — issues
            make_get_issue(installation_id, repo_full_name),
            make_get_all_issues(installation_id, repo_full_name),
            make_create_issue(installation_id, repo_full_name),
            make_update_issue(installation_id, repo_full_name),
            # GitHub — milestones
            make_get_all_milestones(installation_id, repo_full_name),
            make_get_milestone(installation_id, repo_full_name),
            make_create_milestone(installation_id, repo_full_name),
            make_update_milestone(installation_id, repo_full_name),
            # GitHub — labels & comments
            make_add_label(installation_id, repo_full_name),
            make_create_label(installation_id, repo_full_name),
            make_post_comment(installation_id, repo_full_name),
            # AI tools
            make_infer_project_plan(),
            make_compare_plan_vs_state(),
            make_fuzzy_match_milestone(),
            # DB tools
            make_save_onboarding_run(repo_id),
            make_save_file_mapping(repo_id),
        ]

    async def handle(self, context: AgentContext) -> AgentResult:
        """Run the onboarding flow as an autonomous tool-calling loop."""
        log.info(
            "onboarding_start",
            repo=self.repo_full_name,
            event=context.event_type,
        )

        # Build the initial message that kicks off the agent's reasoning
        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": json.dumps({
                    "task": "Onboard this repository",
                    "repo": self.repo_full_name,
                    "event": context.event_type,
                    "instructions": (
                        f"You have been installed on the repository '{self.repo_full_name}'. "
                        "Follow your phase instructions to scan the repo, analyze it, "
                        "fetch existing state, reconcile, execute changes, and persist results. "
                        "Use your tools at each phase. Be thorough but conservative."
                    ),
                }),
            },
        ]

        # Run the autonomous tool-calling loop
        final_text, tool_call_log = await self.run_tool_loop(messages)

        log.info(
            "onboarding_complete",
            repo=self.repo_full_name,
            tool_calls=len(tool_call_log),
        )

        # Parse the final text for structured data if possible
        result_data = {"final_response": final_text, "tool_call_log": tool_call_log}
        try:
            parsed = json.loads(final_text)
            result_data.update(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

        return AgentResult(
            agent_name=self.name,
            status="success",
            actions_taken=[
                {"tool": tc["tool"], "success": tc["result"]["success"]}
                for tc in tool_call_log
            ],
            data=result_data,
            confidence=result_data.get("confidence", 0.7),
            should_notify=True,
            comment_body=self._build_summary_comment(result_data, tool_call_log),
        )

    def _build_summary_comment(self, result_data: dict, tool_call_log: list[dict]) -> str:
        """Build a summary comment about what onboarding did."""
        tool_names = [tc["tool"] for tc in tool_call_log]
        milestones_created = tool_names.count("create_milestone")
        issues_created = tool_names.count("create_issue")
        milestones_updated = tool_names.count("update_milestone")

        lines = [
            "## 🔍 Onboarding Complete",
            "",
            f"I've scanned **{self.repo_full_name}** and set up project tracking.",
            "",
            "### Actions Taken",
            f"- Milestones created: **{milestones_created}**",
            f"- Milestones updated: **{milestones_updated}**",
            f"- Issues created: **{issues_created}**",
            f"- Total tool calls: **{len(tool_call_log)}**",
        ]

        if result_data.get("final_response") and not result_data.get("milestones"):
            lines.extend(["", "### Summary", result_data["final_response"][:1000]])

        lines.extend(["", "---", "*Generated by GitHub Assistant — Onboarding Agent*"])
        return "\n".join(lines)
