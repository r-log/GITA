"""
Issue Analyst Agent — evaluates issues against S.M.A.R.T. criteria.

Includes deterministic logic for:
- Checklist validation: unchecks tasks in Milestone Trackers if sub-issue is still open
- Closure validation: reopens issues closed without evidence of work (no linked PR)
"""

from __future__ import annotations

import re
import json
import structlog

from src.agents.base import BaseAgent, AgentContext, AgentResult
from src.tools.base import Tool
from src.core.github_auth import GitHubClient

# GitHub tools — raw functions for deterministic checks
from src.tools.github.issues import (
    make_get_issue, make_get_all_issues, make_update_issue,
    _get_issue, _update_issue,
)
from src.tools.github.comments import make_post_comment, make_edit_comment, _post_comment
from src.tools.github.labels import make_add_label
from src.tools.github.users import make_tag_user

# AI tools
from src.tools.ai.smart_evaluator import make_evaluate_smart, make_check_milestone_alignment

# DB tools
from src.tools.db.analysis import make_save_evaluation, make_get_previous_evaluation, make_save_analysis

log = structlog.get_logger()

# Pattern to match checklist items: - [x] or - [ ] followed by text and (#N)
CHECKLIST_PATTERN = re.compile(r"- \[([ xX])\] (.+?)\(#(\d+)\)")


class IssueAnalystAgent(BaseAgent):
    """
    Evaluates issues against S.M.A.R.T. criteria, checks milestone alignment,
    and validates Milestone Tracker checklists.
    """

    def __init__(self, installation_id: int, repo_full_name: str, repo_id: int = 0, model: str | None = None):
        tools = self._build_tools(installation_id, repo_full_name, repo_id)

        super().__init__(
            name="issue_analyst",
            description="Issue quality analyst — evaluates issues with S.M.A.R.T. criteria, validates Milestone Tracker checklists",
            tools=tools,
            model=model,
            system_prompt_file="issue_analyst.md",
        )

        self.installation_id = installation_id
        self.repo_full_name = repo_full_name
        self.repo_id = repo_id

    def _build_tools(self, installation_id: int, repo_full_name: str, repo_id: int) -> list[Tool]:
        return [
            make_get_issue(installation_id, repo_full_name),
            make_get_all_issues(installation_id, repo_full_name),
            make_update_issue(installation_id, repo_full_name),
            make_post_comment(installation_id, repo_full_name),
            make_edit_comment(installation_id, repo_full_name),
            make_add_label(installation_id, repo_full_name),
            make_tag_user(installation_id, repo_full_name),
            make_evaluate_smart(),
            make_check_milestone_alignment(),
            make_save_evaluation(repo_id),
            make_get_previous_evaluation(repo_id),
            make_save_analysis(repo_id),
        ]

    async def _validate_checklist(self, issue_number: int, issue_data: dict) -> dict:
        """
        Deterministic checklist validation for Milestone Tracker issues.
        Checks each [x] item — if the linked issue is still open, unchecks it.
        Returns {"fixed": bool, "unchecked": [list of issue numbers]}
        """
        body = issue_data.get("body") or ""
        matches = CHECKLIST_PATTERN.findall(body)

        if not matches:
            return {"fixed": False, "unchecked": []}

        unchecked = []
        new_body = body

        for check_mark, description, linked_number in matches:
            if check_mark.lower() != "x":
                continue  # already unchecked, skip

            # Fetch the linked issue to check its state
            result = await _get_issue(self.installation_id, self.repo_full_name, int(linked_number))
            if not result.success:
                continue

            linked_issue = result.data
            state = linked_issue.get("state", "open")

            if state != "closed":
                # Issue is still open but checked — fix it
                old = f"- [x] {description}(#{linked_number})"
                new = f"- [ ] {description}(#{linked_number})"
                new_body = new_body.replace(old, new)
                # Also try with capital X
                old_cap = f"- [X] {description}(#{linked_number})"
                new_body = new_body.replace(old_cap, new)
                unchecked.append(int(linked_number))
                log.info("checklist_uncheck", issue=issue_number, linked=linked_number, reason="still_open")

        if unchecked:
            # Update the issue body
            update_result = await _update_issue(
                self.installation_id, self.repo_full_name,
                issue_number, body=new_body,
            )
            if update_result.success:
                log.info("checklist_fixed", issue=issue_number, unchecked=unchecked)

                # Post a comment explaining
                issue_list = ", ".join(f"#{n}" for n in unchecked)
                comment = (
                    f"⚠️ **Checklist corrected** — unchecked {issue_list} because "
                    f"{'these issues are' if len(unchecked) > 1 else 'this issue is'} still open. "
                    f"Close the sub-issue{'s' if len(unchecked) > 1 else ''} first, then check them off.\n\n"
                    f"---\n*Generated by GitHub Assistant — Issue Analyst*"
                )
                await _post_comment(self.installation_id, self.repo_full_name, issue_number, comment)

                return {"fixed": True, "unchecked": unchecked}
            else:
                log.warning("checklist_fix_failed", issue=issue_number, error=update_result.error)

        return {"fixed": False, "unchecked": []}

    async def _validate_closure(self, issue_number: int, issue_data: dict) -> dict:
        """
        When an issue is closed, check if there's evidence of actual work:
        - A linked/merged PR that references this issue
        - The issue is a Milestone Tracker (those can be closed when all tasks done)

        If no evidence found, reopen the issue and post a comment.
        Returns {"reopened": bool, "reason": str}
        """
        labels = [l.get("name") if isinstance(l, dict) else l for l in issue_data.get("labels", [])]

        # Don't validate Milestone Tracker closures — those are managed differently
        if "Milestone Tracker" in labels:
            return {"reopened": False, "reason": "milestone_tracker"}

        # Check the issue timeline for cross-referenced PRs
        client = GitHubClient(self.installation_id)
        try:
            events = await client.get(
                f"/repos/{self.repo_full_name}/issues/{issue_number}/timeline",
                params={"per_page": 100},
            )

            has_linked_pr = False
            for event in events:
                # Check for cross-referenced PRs
                if event.get("event") == "cross-referenced":
                    source = event.get("source", {}).get("issue", {})
                    if source.get("pull_request"):
                        has_linked_pr = True
                        break
                # Check for connected/referenced PRs
                if event.get("event") in ("connected", "referenced"):
                    has_linked_pr = True
                    break

            if has_linked_pr:
                log.info("closure_valid", issue=issue_number, reason="has_linked_pr")
                return {"reopened": False, "reason": "has_linked_pr"}

        except Exception as e:
            log.warning("timeline_check_failed", issue=issue_number, error=str(e))
            # If we can't check timeline, don't block — just let it pass
            return {"reopened": False, "reason": "timeline_check_failed"}

        # No PR found — reopen the issue
        log.info("closure_invalid", issue=issue_number, reason="no_linked_pr")

        reopen_result = await _update_issue(
            self.installation_id, self.repo_full_name,
            issue_number, state="open",
        )

        if reopen_result.success:
            comment = (
                f"⚠️ **Issue reopened** — this issue was closed without a linked pull request. "
                f"Please submit a PR that references this issue (e.g. `fixes #{issue_number}`) "
                f"and close the issue through the PR merge.\n\n"
                f"---\n*Generated by GitHub Assistant — Issue Analyst*"
            )
            await _post_comment(self.installation_id, self.repo_full_name, issue_number, comment)
            return {"reopened": True, "reason": "no_linked_pr"}

        return {"reopened": False, "reason": "reopen_failed"}

    async def handle(self, context: AgentContext) -> AgentResult:
        log.info(
            "issue_analyst_start",
            repo=self.repo_full_name,
            webhook_event=context.event_type,
        )

        issue_data = context.event_payload.get("issue", {})
        issue_number = issue_data.get("number", 0)
        labels = [l.get("name") if isinstance(l, dict) else l for l in issue_data.get("labels", [])]
        is_tracker = "Milestone Tracker" in labels
        is_edit = "edited" in context.event_type
        is_closed = "closed" in context.event_type

        # DETERMINISTIC: If an issue was closed, validate that work was actually done
        if is_closed:
            closure_result = await self._validate_closure(issue_number, issue_data)
            return AgentResult(
                agent_name=self.name,
                status="success",
                actions_taken=[{"action": "closure_validated", **closure_result}],
                data={"closure_result": closure_result, "issue_number": issue_number},
                confidence=1.0,
                should_notify=closure_result.get("reopened", False),
            )

        # DETERMINISTIC: If a Milestone Tracker was edited, validate the checklist in code
        if is_tracker and is_edit:
            checklist_result = await self._validate_checklist(issue_number, issue_data)
            if checklist_result["fixed"]:
                # Checklist was fixed — that's all we need to do for this event
                return AgentResult(
                    agent_name=self.name,
                    status="success",
                    actions_taken=[{"action": "checklist_validated", "unchecked": checklist_result["unchecked"]}],
                    data={"checklist_result": checklist_result, "issue_number": issue_number},
                    confidence=1.0,
                    should_notify=True,
                )

        # LLM-BASED: For everything else, run the S.M.A.R.T. evaluation via tool loop
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
                        "labels": labels,
                        "assignees": [a.get("login") if isinstance(a, dict) else a for a in issue_data.get("assignees", [])],
                        "is_milestone_tracker": is_tracker,
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
