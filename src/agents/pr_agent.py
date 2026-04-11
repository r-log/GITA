"""
PR Review Agent — analyzes pull requests for quality and milestone alignment.

Architecture: gather-then-reason.
  1. Python gathers all data (PR files, diff, blast radius, AI analysis)
  2. LLM receives assembled context and reasons about findings
  3. LLM uses output tools (post_comment, create_check_run) to report
"""

from __future__ import annotations

import json
import structlog

from src.agents.base import BaseAgent, AgentContext, AgentResult
from src.tools.base import Tool

# GitHub tools — raw functions for data gathering
from src.tools.github.pull_requests import (
    make_get_pr, make_get_open_prs,
    _get_pr_diff, _get_pr_files,
)
from src.tools.github.issues import make_get_issue, make_get_all_issues
from src.tools.github.milestones import make_get_milestone

# GitHub tools — output (LLM decides when to use these)
from src.tools.github.comments import make_post_comment
from src.tools.github.checks import make_create_check_run
from src.tools.github.users import make_tag_user

# AI tools — raw functions for analysis
from src.tools.ai.code_analyzer import _analyze_diff_quality, _check_test_coverage

# AI tools — LLM-callable
from src.tools.ai.smart_evaluator import make_check_milestone_alignment

# DB tools
from src.tools.db.analysis import make_save_analysis
from src.tools.db.rag_queries import make_get_pr_full, make_search_comments, make_get_pr_reviews, make_search_events
from src.tools.db.graph_queries import (
    _get_blast_radius, _get_file_ownership, _get_focused_code_map,
)

log = structlog.get_logger()


class PRReviewAgent(BaseAgent):
    """
    Analyzes pull requests for code quality, test coverage,
    linked issue verification, and milestone alignment.
    """

    def __init__(self, installation_id: int, repo_full_name: str, repo_id: int = 0, model: str | None = None):
        # LLM only gets output + lookup tools — no gathering tools
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
        """LLM only gets output and lookup tools — gathering is done in Python."""
        return [
            # Lookup (LLM may need to fetch a linked issue or milestone)
            make_get_pr(installation_id, repo_full_name),
            make_get_issue(installation_id, repo_full_name),
            make_get_all_issues(installation_id, repo_full_name),
            make_get_milestone(installation_id, repo_full_name),
            make_get_open_prs(installation_id, repo_full_name),
            # AI reasoning
            make_check_milestone_alignment(),
            # Output (LLM decides what to post)
            make_post_comment(installation_id, repo_full_name, repo_id),
            make_create_check_run(installation_id, repo_full_name),
            make_tag_user(installation_id, repo_full_name),
            # DB
            make_save_analysis(repo_id),
            # RAG
            make_get_pr_full(repo_id),
            make_search_comments(repo_id),
            make_get_pr_reviews(repo_id),
            make_search_events(repo_id),
        ]

    async def _gather_context(self, pr_number: int, shared_data: dict | None = None) -> dict:
        """
        Gather all PR data in Python before the LLM call.
        Uses shared_data from Supervisor if available (avoids duplicate API calls
        when running in parallel with Risk Detective).
        """
        gathered = {}

        # Use shared data from Supervisor if available, otherwise fetch
        if shared_data and shared_data.get("files"):
            gathered["files"] = shared_data["files"]
            gathered["diff"] = shared_data.get("diff", "")
            gathered["blast_radius"] = shared_data.get("blast_radius", {})
        else:
            # Fallback: fetch independently
            files_result = await _get_pr_files(
                self.installation_id, self.repo_full_name, pr_number, self.repo_id,
            )
            gathered["files"] = files_result.data if files_result.success else []

            diff_result = await _get_pr_diff(self.installation_id, self.repo_full_name, pr_number)
            gathered["diff"] = diff_result.data.get("diff", "") if diff_result.success else ""

            file_paths = [f["filename"] for f in gathered["files"]] if gathered["files"] else []
            if file_paths:
                blast_result = await _get_blast_radius(self.repo_id, file_paths, depth=2)
                gathered["blast_radius"] = blast_result.data if blast_result.success else {}
            else:
                gathered["blast_radius"] = {}

        diff_text = gathered["diff"][:30000]  # Cap for LLM context

        # PR-specific gathering (not shared — only PR Reviewer needs these)
        file_paths = [f["filename"] for f in gathered["files"]] if gathered["files"] else []
        if file_paths:
            ownership_result = await _get_file_ownership(self.repo_id, file_paths)
            gathered["file_ownership"] = ownership_result.data if ownership_result.success else {}

            code_map_result = await _get_focused_code_map(self.repo_id, file_paths, depth=1)
            gathered["focused_code_map"] = code_map_result.data if code_map_result.success else ""
        else:
            gathered["file_ownership"] = {}
            gathered["focused_code_map"] = ""

        # AI analysis
        pr_info = {"files": gathered["files"], "file_count": len(gathered["files"])}
        if diff_text:
            quality_result = await _analyze_diff_quality(diff_text, pr_info)
            gathered["quality_analysis"] = quality_result.data if quality_result.success else {}

            coverage_result = await _check_test_coverage(diff_text, gathered["files"])
            gathered["test_coverage"] = coverage_result.data if coverage_result.success else {}
        else:
            gathered["quality_analysis"] = {}
            gathered["test_coverage"] = {}

        return gathered

    async def handle(self, context: AgentContext) -> AgentResult:
        log.info(
            "pr_reviewer_start",
            repo=self.repo_full_name,
            webhook_event=context.event_type,
        )

        pr_data = context.event_payload.get("pull_request", {})
        pr_number = pr_data.get("number", 0)

        # Phase 1: Gather all data in Python (no LLM tool calls needed)
        # Check for shared data from Supervisor (avoids duplicate API calls with Risk Detective)
        shared_data = context.additional_data.get("pr_gathered")
        log.info("pr_reviewer_gathering", pr=pr_number, shared=bool(shared_data))
        gathered = await self._gather_context(pr_number, shared_data)

        # Phase 2: Send everything to LLM for reasoning + output decisions
        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": json.dumps({
                    "task": "Review this pull request and report findings",
                    "repo": self.repo_full_name,
                    "pr_number": pr_number,
                    "pr_summary": {
                        "title": pr_data.get("title"),
                        "body": (pr_data.get("body") or "")[:2000],
                        "author": pr_data.get("user", {}).get("login"),
                        "base": pr_data.get("base", {}).get("ref"),
                        "head": pr_data.get("head", {}).get("ref"),
                        "head_sha": pr_data.get("head", {}).get("sha"),
                        "additions": pr_data.get("additions"),
                        "deletions": pr_data.get("deletions"),
                        "changed_files": pr_data.get("changed_files"),
                    },
                    "gathered_data": {
                        "files_changed": gathered["files"],
                        "diff": gathered["diff"],
                        "blast_radius": gathered.get("blast_radius", {}),
                        "file_ownership": gathered.get("file_ownership", {}),
                        "quality_analysis": gathered.get("quality_analysis", {}),
                        "test_coverage": gathered.get("test_coverage", {}),
                    },
                    "focused_code_map": gathered.get("focused_code_map", ""),
                    "instructions": (
                        f"All data has been gathered for PR #{pr_number}. "
                        "Review the quality analysis, test coverage, blast radius, and file ownership above. "
                        "Create a check run with your verdict and post a comment if there are findings. "
                        "If the PR body references an issue (fixes #N, closes #N), fetch it with get_issue to verify linkage."
                    ),
                }, default=str),
            },
        ]

        final_text, tool_call_log = await self.run_tool_loop(messages)

        log.info(
            "pr_reviewer_complete",
            repo=self.repo_full_name,
            pr=pr_number,
            tool_calls=len(tool_call_log),
        )

        # Derive severity from quality analysis + test coverage
        quality = gathered.get("quality_analysis", {}) or {}
        coverage = gathered.get("test_coverage", {}) or {}
        severity = "info"
        if quality.get("critical_issues") or coverage.get("missing_tests"):
            severity = "warning"

        warned_files = sorted({
            f.get("filename", "") for f in gathered.get("files", []) if f.get("filename")
        })

        data = {
            "final_response": final_text,
            "tool_call_log": tool_call_log,
            "pr_number": pr_number,
        }

        if pr_number and severity == "warning":
            data["outcome_predictions"] = [
                {
                    "outcome_type": "risk_warning",
                    "target_type": "pr",
                    "target_number": pr_number,
                    "predicted": {
                        "severity": severity,
                        "file_paths_warned": warned_files,
                    },
                },
            ]

        return AgentResult(
            agent_name=self.name,
            status="success",
            actions_taken=[
                {"tool": tc["tool"], "success": tc["result"]["success"]}
                for tc in tool_call_log
            ],
            data=data,
            confidence=0.8,
            should_notify=any(tc["tool"] in ("post_comment", "create_check_run") for tc in tool_call_log),
        )
