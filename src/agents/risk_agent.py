"""
Risk Detective Agent — scans for security risks, breaking changes, and dependency issues.

Architecture: gather-then-reason.
  1. Python gathers all data (diff, files, blast radius, security scans)
  2. LLM receives assembled findings and reasons about severity
  3. LLM uses output tools (create_check_run, post_comment, tag_user) to report
"""

from __future__ import annotations

import json
import structlog

from src.agents.base import BaseAgent, AgentContext, AgentResult
from src.tools.base import Tool

# GitHub tools — raw functions for data gathering
from src.tools.github.pull_requests import _get_pr_diff, _get_pr_files, _get_open_prs

# GitHub tools — output (LLM decides when to use these)
from src.tools.github.pull_requests import make_get_pr, make_get_open_prs
from src.tools.github.repos import make_read_file
from src.tools.github.comments import make_post_comment
from src.tools.github.checks import make_create_check_run
from src.tools.github.users import make_tag_user

# AI tools — raw functions for scanning
from src.tools.ai.risk_scanner import (
    _scan_secrets, _scan_security_patterns,
    _detect_breaking_changes, _check_dependency_changes,
)

# DB tools
from src.tools.db.analysis import make_save_analysis
from src.tools.db.graph_queries import _get_blast_radius, _get_file_dependents

log = structlog.get_logger()

# Dependency file patterns
_DEP_FILES = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "requirements.txt", "pyproject.toml", "Pipfile.lock", "go.mod", "go.sum",
    "Cargo.toml", "Cargo.lock", "Gemfile.lock", "composer.lock",
}


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
        """LLM only gets output and lookup tools — gathering + scanning is done in Python."""
        return [
            # Lookup (LLM may need to read a specific file for context)
            make_get_pr(installation_id, repo_full_name),
            make_read_file(installation_id, repo_full_name),
            make_get_open_prs(installation_id, repo_full_name),
            # Output
            make_post_comment(installation_id, repo_full_name),
            make_create_check_run(installation_id, repo_full_name),
            make_tag_user(installation_id, repo_full_name),
            # DB
            make_save_analysis(repo_id),
        ]

    async def _gather_context(self, pr_number: int, shared_data: dict | None = None) -> dict:
        """
        Gather all risk data in Python before the LLM call.
        Uses shared_data from Supervisor if available (avoids duplicate API calls
        when running in parallel with PR Reviewer).
        """
        gathered: dict = {}

        # Use shared data from Supervisor if available, otherwise fetch
        if shared_data and shared_data.get("files"):
            gathered["files"] = shared_data["files"]
            gathered["diff"] = shared_data.get("diff", "")
            gathered["blast_radius"] = shared_data.get("blast_radius", {})
        else:
            # Fetch independently (fallback for push events or standalone runs)
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

        diff_text = gathered["diff"][:30000]  # Cap for scanning

        # Run all security scans
        if diff_text:
            secrets_result = await _scan_secrets(diff_text)
            gathered["secrets_scan"] = secrets_result.data if secrets_result.success else {}

            patterns_result = await _scan_security_patterns(diff_text)
            gathered["security_patterns"] = patterns_result.data if patterns_result.success else {}

            breaking_result = await _detect_breaking_changes(diff_text, gathered["files"])
            gathered["breaking_changes"] = breaking_result.data if breaking_result.success else {}
        else:
            gathered["secrets_scan"] = {}
            gathered["security_patterns"] = {}
            gathered["breaking_changes"] = {}

        # Check dependency changes if relevant files were touched
        file_names = {f.get("filename", "").split("/")[-1] for f in gathered["files"]}
        if file_names & _DEP_FILES and diff_text:
            dep_result = await _check_dependency_changes(diff_text)
            gathered["dependency_changes"] = dep_result.data if dep_result.success else {}
        else:
            gathered["dependency_changes"] = {}

        # For breaking changes, find dependents via graph
        if gathered.get("breaking_changes"):
            file_paths = [f["filename"] for f in gathered["files"]] if gathered["files"] else []
            dependents = {}
            for fp in file_paths[:10]:  # Cap to avoid excessive queries
                dep_result = await _get_file_dependents(self.repo_id, fp)
                if dep_result.success and dep_result.data.get("count", 0) > 0:
                    dependents[fp] = dep_result.data
            gathered["file_dependents"] = dependents

        # Check for merge conflicts with other open PRs
        prs_result = await _get_open_prs(self.installation_id, self.repo_full_name)
        if prs_result.success:
            gathered["other_open_prs"] = max(len(prs_result.data or []) - 1, 0)

        return gathered

    async def handle(self, context: AgentContext) -> AgentResult:
        log.info(
            "risk_detective_start",
            repo=self.repo_full_name,
            webhook_event=context.event_type,
        )

        pr_data = context.event_payload.get("pull_request", {})
        pr_number = pr_data.get("number", 0)

        # Phase 1: Gather all data in Python
        # Check for shared data from Supervisor (avoids duplicate API calls with PR Reviewer)
        shared_data = context.additional_data.get("pr_gathered")
        log.info("risk_detective_gathering", pr=pr_number, shared=bool(shared_data))
        gathered = await self._gather_context(pr_number, shared_data)

        # Phase 2: Send everything to LLM for reasoning + severity assessment
        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": json.dumps({
                    "task": "Assess risks in this PR and report findings",
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
                    "scan_results": {
                        "secrets": gathered.get("secrets_scan", {}),
                        "security_patterns": gathered.get("security_patterns", {}),
                        "breaking_changes": gathered.get("breaking_changes", {}),
                        "dependency_changes": gathered.get("dependency_changes", {}),
                    },
                    "impact": {
                        "blast_radius": gathered.get("blast_radius", {}),
                        "file_dependents": gathered.get("file_dependents", {}),
                        "other_open_prs": gathered.get("other_open_prs", 0),
                    },
                    "files_changed": gathered.get("files", []),
                    "instructions": (
                        f"All security scans and impact analysis have been completed for PR #{pr_number}. "
                        "Review the scan results above. Determine severity (critical/warning/info). "
                        "Create a check run: failure if critical, neutral if warnings, success if clean. "
                        "If critical, tag maintainers with tag_user. Post a comment with findings."
                    ),
                }, default=str),
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
