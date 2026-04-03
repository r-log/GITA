"""
Onboarding Agent — first-run setup when the app is installed on a new repo.

Uses a multi-pass architecture:
  Pass 1: Structure — tree + manifests → project summary + files_to_read
  Pass 2: Deep Dive — read key files → feature summaries + gaps
  Pass 2.5: Reconciliation — fetch existing issues (no LLM)
  Pass 3: Milestones — compressed scratchpad → milestone plan
  Pass 4: Issues — create sub-issues + Milestone Tracker issues
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import structlog
from thefuzz import fuzz

from src.agents.base import BaseAgent, AgentContext, AgentResult
from src.core.config import settings
from src.tools.base import Tool

# GitHub tools
from src.tools.github.repos import make_get_repo_tree, make_read_file, make_get_collaborators, _get_repo_tree, _read_file, _get_collaborators
from src.tools.github.issues import make_get_issue, make_get_all_issues, make_create_issue, make_update_issue, _get_all_issues
from src.tools.github.labels import make_add_label, make_create_label
from src.tools.github.comments import make_post_comment

# AI tools
from src.tools.ai.project_planner import (
    make_infer_project_plan,
    make_compare_plan_vs_state,
)

# DB tools
from src.tools.db.onboarding import make_save_onboarding_run, make_save_file_mapping, _save_onboarding_run

log = structlog.get_logger()

# Manifest / high-value files to always read in Pass 1
MANIFEST_PATTERNS = {
    "readme.md", "readme", "readme.rst", "readme.txt",
    "package.json", "pyproject.toml", "cargo.toml", "go.mod", "go.sum",
    "composer.json", "gemfile", "build.gradle", "pom.xml",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "makefile", "procfile",
    ".github/workflows",  # prefix match
}

MAX_PASS2_CHARS = 40000  # char budget for Pass 2 file contents


def _extract_json(text: str) -> str:
    """
    Extract JSON from LLM response, handling:
    - Pure JSON
    - JSON wrapped in ```json ... ``` code fences
    - JSON embedded in prose text with code fences
    - JSON with preamble text before the opening brace
    """
    text = text.strip()

    # Try direct parse first
    if text.startswith("{") or text.startswith("["):
        return text

    # Look for ```json ... ``` block embedded in text
    import re
    fence_match = re.search(r"```(?:json)?\s*\n(\{[\s\S]*?\})\s*```", text)
    if fence_match:
        return fence_match.group(1).strip()

    # Strip leading/trailing fences only
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return text

    # Last resort: find first { and last } in the text
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        return text[first_brace:last_brace + 1]

    return text


class OnboardingAgent(BaseAgent):
    """
    Multi-pass onboarding agent. Each pass produces a compressed artifact
    that the next pass consumes via an in-memory scratchpad.
    """

    def __init__(self, installation_id: int, repo_full_name: str, repo_id: int = 0, model: str | None = None):
        # Build all tool groups
        self._scan_tools = [
            make_get_repo_tree(installation_id, repo_full_name),
            make_read_file(installation_id, repo_full_name),
        ]
        self._read_tools = [
            make_read_file(installation_id, repo_full_name),
        ]
        self._plan_tools = [
            make_compare_plan_vs_state(),
        ]
        self._issue_tools = [
            make_create_issue(installation_id, repo_full_name),
            make_update_issue(installation_id, repo_full_name),
            make_create_label(installation_id, repo_full_name),
            make_add_label(installation_id, repo_full_name),
            make_post_comment(installation_id, repo_full_name),
            make_save_onboarding_run(repo_id),
            make_save_file_mapping(repo_id),
        ]

        # Initialize with all tools (base class needs them for registration)
        all_tools = self._scan_tools + self._read_tools + self._plan_tools + self._issue_tools
        # Deduplicate by name (read_file appears in both scan and read)
        seen = set()
        unique_tools = []
        for t in all_tools:
            if t.name not in seen:
                seen.add(t.name)
                unique_tools.append(t)

        super().__init__(
            name="onboarding",
            description="Project setup specialist — scans repos, creates Milestone Tracker issues with linked sub-issues",
            tools=unique_tools,
            system_prompt_file="onboarding.md",
        )

        self.installation_id = installation_id
        self.repo_full_name = repo_full_name
        self.repo_id = repo_id

        # Load per-pass prompts
        self._pass_prompts: dict[str, str] = {}
        for pass_name in ["pass1_structure", "pass2_deepdive", "pass3_milestones", "pass3_5_validation", "pass4_issues"]:
            prompt_path = Path("prompts") / f"onboarding_{pass_name}.md"
            if prompt_path.exists():
                self._pass_prompts[pass_name] = prompt_path.read_text(encoding="utf-8")
            else:
                raise FileNotFoundError(f"Pass prompt not found: {prompt_path}")

    async def _run_pass(
        self,
        pass_name: str,
        system_prompt: str,
        user_content: str,
        tools: list[Tool],
        max_calls: int = 20,
        model: str | None = None,
    ) -> tuple[str, list[dict]]:
        """
        Run a single pass: temporarily swap tools and model, call run_tool_loop, restore.
        """
        original_tools = self.tools
        original_tool_map = self._tool_map
        original_model = self.model

        try:
            self.tools = tools
            self._tool_map = {t.name: t for t in tools}
            if model:
                self.model = model

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]

            log.info("pass_start", agent=self.name, pass_name=pass_name, model=self.model, tools=[t.name for t in tools])
            final_text, tool_call_log = await self.run_tool_loop(messages, max_calls=max_calls)
            log.info("pass_complete", agent=self.name, pass_name=pass_name, tool_calls=len(tool_call_log))

            return final_text, tool_call_log
        finally:
            self.tools = original_tools
            self._tool_map = original_tool_map
            self.model = original_model

    async def _llm_call(self, system_prompt: str, user_content: str, model: str | None = None) -> str:
        """Direct LLM call without tool loop (for pure reasoning passes)."""
        use_model = model or self.model
        log.info("llm_call", agent=self.name, model=use_model)
        response = await self._client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
        return _extract_json(raw)

    def _is_manifest(self, path: str) -> bool:
        """Check if a file path matches a manifest/high-value pattern."""
        lower = path.lower()
        filename = lower.split("/")[-1]

        if filename in MANIFEST_PATTERNS:
            return True
        # Prefix matches (e.g. .github/workflows/*)
        for pattern in MANIFEST_PATTERNS:
            if lower.startswith(pattern):
                return True
        return False

    # ── Pass 1: Structure ──────────────────────────────────────────────

    async def _pass1_structure(self) -> dict[str, Any]:
        """
        Read tree + manifests, ask LLM to summarize the project and pick files to read.
        """
        log.info("pass1_start", repo=self.repo_full_name)

        # Get full file tree
        tree_result = await _get_repo_tree(self.installation_id, self.repo_full_name)
        if not tree_result.success:
            raise RuntimeError(f"Failed to read repo tree: {tree_result.error}")

        tree = tree_result.data
        file_paths = [f["path"] for f in tree if f["type"] == "blob"]

        # Build tree listing
        tree_listing = f"# Repository: {self.repo_full_name}\n"
        tree_listing += f"## File Tree ({len(file_paths)} files)\n```\n"
        for f in tree:
            if f["type"] == "blob":
                tree_listing += f"  {f['path']} ({f.get('size', 0)} bytes)\n"
            else:
                tree_listing += f"  {f['path']}/\n"
        tree_listing += "```\n"

        # Identify and read manifest files in parallel
        manifest_paths = [p for p in file_paths if self._is_manifest(p)]
        log.info("pass1_reading_manifests", count=len(manifest_paths))

        read_tasks = [
            _read_file(self.installation_id, self.repo_full_name, p)
            for p in manifest_paths
        ]
        results = await asyncio.gather(*read_tasks, return_exceptions=True)

        manifest_contents = "\n## Manifest Files\n"
        for path, result in zip(manifest_paths, results):
            if isinstance(result, Exception) or not result.success:
                continue
            content = result.data.get("content", "")
            # Truncate very large manifests (e.g. a huge README)
            if len(content) > 5000:
                content = content[:5000] + "\n... [truncated]"
            manifest_contents += f"### {path}\n```\n{content}\n```\n\n"

        context = tree_listing + manifest_contents

        log.info("pass1_context_size", chars=len(context))

        # LLM call — pure reasoning, no tools needed
        raw = await self._llm_call(
            self._pass_prompts["pass1_structure"],
            context,
            model=settings.ai_model_onboarding_pass1,
        )

        try:
            result = json.loads(_extract_json(raw))
        except json.JSONDecodeError:
            log.error("pass1_json_parse_failed", raw=raw[:500])
            raise RuntimeError("Pass 1 failed: LLM returned invalid JSON")

        # Validate files_to_read against actual tree
        valid_paths = set(file_paths)
        original_count = len(result.get("files_to_read", []))
        result["files_to_read"] = [
            p for p in result.get("files_to_read", [])
            if p in valid_paths
        ]
        filtered = original_count - len(result["files_to_read"])
        if filtered > 0:
            log.warn("pass1_filtered_hallucinated_paths", count=filtered)

        # Store the tree for later reference
        result["_tree"] = tree

        log.info(
            "pass1_complete",
            project=result.get("project_name"),
            files_to_read=len(result["files_to_read"]),
        )
        return result

    # ── Pass 2: Deep Dive ──────────────────────────────────────────────

    async def _pass2_deep_dive(self, scratchpad: dict) -> dict[str, Any]:
        """
        Read selected key files, ask LLM to summarize features and gaps.
        LLM can also read additional files via tool calls.
        """
        files_to_read = scratchpad["structure"]["files_to_read"]
        log.info("pass2_start", files_count=len(files_to_read))

        # Read all selected files in parallel
        read_tasks = [
            _read_file(self.installation_id, self.repo_full_name, p)
            for p in files_to_read
        ]
        results = await asyncio.gather(*read_tasks, return_exceptions=True)

        # Build context with file contents, respecting char budget
        context_parts = [
            f"# Project: {scratchpad['structure'].get('project_name', self.repo_full_name)}\n",
            f"## Initial Assessment\n{scratchpad['structure'].get('initial_assessment', 'N/A')}\n\n",
            f"## Stack\n{json.dumps(scratchpad['structure'].get('stack', {}), indent=2)}\n\n",
            "## Key Files\n\n",
        ]
        total_chars = sum(len(p) for p in context_parts)

        files_included = 0
        for path, result in zip(files_to_read, results):
            if isinstance(result, Exception) or not result.success:
                continue

            content = result.data.get("content", "")
            file_block = f"### {path}\n```\n{content}\n```\n\n"

            if total_chars + len(file_block) > MAX_PASS2_CHARS:
                # Truncate
                truncated = content[:2000] + "\n... [truncated]"
                file_block = f"### {path}\n```\n{truncated}\n```\n\n"
                if total_chars + len(file_block) > MAX_PASS2_CHARS:
                    break

            context_parts.append(file_block)
            total_chars += len(file_block)
            files_included += 1

        context = "".join(context_parts)
        log.info("pass2_context_size", chars=len(context), files_included=files_included)

        # Tool loop — LLM can read_file for additional files it discovers
        raw, tool_call_log = await self._run_pass(
            "pass2",
            self._pass_prompts["pass2_deepdive"],
            context,
            tools=self._read_tools,
            max_calls=30,
            model=settings.ai_model_onboarding_pass2,
        )

        try:
            result = json.loads(_extract_json(raw))
        except json.JSONDecodeError:
            log.error("pass2_json_parse_failed", raw=raw[:500])
            raise RuntimeError("Pass 2 failed: LLM returned invalid JSON")

        log.info(
            "pass2_complete",
            features=len(result.get("features_found", [])),
            gaps=len(result.get("gaps_found", [])),
            extra_reads=len(tool_call_log),
        )
        return result

    # ── Pass 2.5: Fetch Existing State ─────────────────────────────────

    async def _pass2_5_reconciliation(self) -> dict[str, Any]:
        """
        Fetch existing issues and collaborators. No LLM call — pure data fetch.
        """
        log.info("pass2_5_start", repo=self.repo_full_name)

        issues_result, collab_result = await asyncio.gather(
            _get_all_issues(self.installation_id, self.repo_full_name),
            _get_collaborators(self.installation_id, self.repo_full_name),
        )

        existing_issues = issues_result.data if issues_result.success else []
        collaborators = collab_result.data if collab_result.success else []

        log.info(
            "pass2_5_complete",
            issues=len(existing_issues),
            collaborators=len(collaborators),
        )

        return {
            "existing_issues": existing_issues,
            "collaborators": collaborators,
        }

    # ── Pass 3: Milestones ─────────────────────────────────────────────

    async def _pass3_milestones(self, scratchpad: dict) -> dict[str, Any]:
        """
        Infer milestones from compressed scratchpad. Direct LLM call, no tools.
        """
        log.info("pass3_start")

        # Build compressed context from all previous passes
        structure = scratchpad["structure"]
        deep_dive = scratchpad["deep_dive"]
        existing = scratchpad["existing"]

        context_parts = [
            f"# Project: {structure.get('project_name', self.repo_full_name)}\n",
            f"**Purpose:** {structure.get('project_purpose', 'Unknown')}\n\n",
            f"## Stack\n{json.dumps(structure.get('stack', {}), indent=2)}\n\n",
            f"## Architecture: {structure.get('architecture_pattern', 'unknown')}\n\n",
            f"## Key Directories\n{json.dumps(structure.get('key_directories', {}), indent=2)}\n\n",
        ]

        # Features found
        context_parts.append("## Features Found\n")
        for feat in deep_dive.get("features_found", []):
            status = feat.get("status", "unknown")
            context_parts.append(
                f"- **{feat.get('name', '?')}** [{status}]: {feat.get('evidence', '')}\n"
            )
            if feat.get("gaps"):
                context_parts.append(f"  Gaps: {feat['gaps']}\n")

        # Gaps found
        context_parts.append("\n## Gaps Found\n")
        for gap in deep_dive.get("gaps_found", []):
            context_parts.append(
                f"- **{gap.get('area', '?')}** [{gap.get('severity', '?')}]: {gap.get('details', '')}\n"
            )

        # Tech details
        context_parts.append(f"\n## Tech Details\n{json.dumps(deep_dive.get('tech_details', {}), indent=2)}\n\n")

        # Existing issues
        issues = existing.get("existing_issues", [])
        if issues:
            context_parts.append(f"\n## Existing Issues ({len(issues)} open)\n")
            for issue in issues:
                labels = ", ".join(l.get("name", "") for l in issue.get("labels", []))
                context_parts.append(
                    f"- #{issue.get('number', '?')} {issue.get('title', '?')} [{labels}]\n"
                )

        context = "".join(context_parts)
        log.info("pass3_context_size", chars=len(context))

        raw = await self._llm_call(
            self._pass_prompts["pass3_milestones"],
            context,
            model=settings.ai_model_onboarding_pass3,
        )

        try:
            result = json.loads(_extract_json(raw))
        except json.JSONDecodeError:
            log.error("pass3_json_parse_failed", raw=raw[:500])
            raise RuntimeError("Pass 3 failed: LLM returned invalid JSON")

        log.info(
            "pass3_complete",
            milestones=len(result.get("milestones", [])),
            confidence=result.get("overall_confidence"),
        )
        return result

    # ── Pass 3.5: Validate Plan ─────────────────────────────────────────

    async def _pass3_5_validate(self, scratchpad: dict) -> dict[str, Any]:
        """
        Validate milestone plan before issue creation.
        Stage A: deterministic checks (file existence, fuzzy dedup).
        Stage B: LLM spot-check of ambiguous items.
        """
        log.info("pass3_5_start")

        milestones_data = scratchpad["milestones"]
        tree_paths = {
            f["path"] for f in scratchpad["structure"].get("_tree", [])
            if f["type"] == "blob"
        }
        existing_issues = scratchpad["existing"].get("existing_issues", [])

        # Stage A: Deterministic checks
        flags: list[dict] = []
        auto_skipped = 0
        auto_corrected = 0

        for milestone in milestones_data.get("milestones", []):
            for task in milestone.get("tasks", []):
                task_files = task.get("files", [])
                task_title = task.get("title", "")
                task_status = task.get("status", "not-started")

                # 1. File existence check
                if task_files:
                    files_exist = [f for f in task_files if f in tree_paths]
                    files_missing = [f for f in task_files if f not in tree_paths]

                    # Task says "not-started" but all referenced files exist
                    if task_status == "not-started" and files_exist and len(files_exist) == len(task_files):
                        flags.append({
                            "milestone_title": milestone.get("title", ""),
                            "task_title": task_title,
                            "flag_type": "status_mismatch",
                            "details": f"Files exist: {files_exist}",
                            "files_to_check": files_exist[:3],  # limit spot-check
                        })

                    # Task references files that don't exist at all
                    if files_missing and not files_exist:
                        flags.append({
                            "milestone_title": milestone.get("title", ""),
                            "task_title": task_title,
                            "flag_type": "files_missing",
                            "details": f"None of the referenced files exist: {files_missing}",
                        })

                # 2. Fuzzy dedup against existing issues
                if existing_issues:
                    best_score = 0
                    best_match = None
                    for issue in existing_issues:
                        score = fuzz.ratio(task_title.lower(), issue.get("title", "").lower())
                        if score > best_score:
                            best_score = score
                            best_match = issue

                    if best_score >= 80:
                        # Clear duplicate — auto-skip
                        task["_validation"] = "skip"
                        task["_skip_reason"] = f"Duplicate of #{best_match['number']}: {best_match['title']} (score={best_score})"
                        auto_skipped += 1
                        log.info("pass3_5_auto_skip", task=task_title, duplicate_of=best_match["number"], score=best_score)
                    elif best_score >= 50:
                        flags.append({
                            "milestone_title": milestone.get("title", ""),
                            "task_title": task_title,
                            "flag_type": "possible_duplicate",
                            "details": f"Similar to #{best_match['number']}: {best_match['title']} (score={best_score})",
                            "existing_issue": {"number": best_match["number"], "title": best_match["title"]},
                        })

        log.info("pass3_5_stage_a_complete", flags=len(flags), auto_skipped=auto_skipped)

        # Stage B: LLM spot-check if there are flagged items
        if flags:
            context = json.dumps({
                "flagged_items": flags,
                "project_name": scratchpad["structure"].get("project_name", ""),
            }, indent=2)

            raw, tool_call_log = await self._run_pass(
                "pass3_5",
                self._pass_prompts["pass3_5_validation"],
                context,
                tools=self._read_tools,
                max_calls=10,
                model=settings.ai_model_onboarding_pass3_5,
            )

            try:
                validation_result = json.loads(_extract_json(raw))
                decisions = validation_result.get("decisions", [])

                # Apply LLM decisions
                for decision in decisions:
                    d_milestone = decision.get("milestone_title", "")
                    d_task = decision.get("task_title", "")
                    action = decision.get("action", "keep")

                    for milestone in milestones_data.get("milestones", []):
                        if milestone.get("title", "") != d_milestone:
                            continue
                        for task in milestone.get("tasks", []):
                            if task.get("title", "") != d_task:
                                continue

                            if action == "skip":
                                task["_validation"] = "skip"
                                task["_skip_reason"] = decision.get("reason", "LLM determined duplicate/invalid")
                                auto_skipped += 1
                            elif action == "update_status":
                                old_status = task.get("status")
                                task["status"] = decision.get("new_status", task["status"])
                                if decision.get("new_labels"):
                                    task["labels"] = decision["new_labels"]
                                auto_corrected += 1
                                log.info("pass3_5_status_corrected", task=d_task, old=old_status, new=task["status"])

                log.info("pass3_5_llm_decisions", decisions=len(decisions))
            except json.JSONDecodeError:
                log.error("pass3_5_json_parse_failed", raw=raw[:500])

        # Remove skipped tasks from milestones
        for milestone in milestones_data.get("milestones", []):
            original_count = len(milestone.get("tasks", []))
            milestone["tasks"] = [
                t for t in milestone.get("tasks", [])
                if t.get("_validation") != "skip"
            ]
            removed = original_count - len(milestone["tasks"])
            if removed:
                log.info("pass3_5_tasks_removed", milestone=milestone.get("title"), removed=removed)

        # Remove empty milestones (all tasks skipped)
        original_milestone_count = len(milestones_data.get("milestones", []))
        milestones_data["milestones"] = [
            m for m in milestones_data.get("milestones", [])
            if m.get("tasks")
        ]
        removed_milestones = original_milestone_count - len(milestones_data["milestones"])

        log.info(
            "pass3_5_complete",
            tasks_skipped=auto_skipped,
            tasks_corrected=auto_corrected,
            milestones_removed=removed_milestones,
            milestones_remaining=len(milestones_data["milestones"]),
        )

        return milestones_data

    # ── Pass 4: Create Issues ──────────────────────────────────────────

    async def _pass4_issues(self, scratchpad: dict) -> tuple[str, list[dict]]:
        """
        Create sub-issues and Milestone Tracker issues using the tool loop.
        """
        log.info("pass4_start")

        milestones = scratchpad["milestones"]
        deep_dive = scratchpad["deep_dive"]
        existing = scratchpad["existing"]

        # Build context for the issue-creation LLM
        context_parts = [
            f"# Milestone Plan for {self.repo_full_name}\n\n",
            f"## Project Summary\n{milestones.get('project_summary', 'N/A')}\n\n",
            f"## Milestones to Create\n\n{json.dumps(milestones.get('milestones', []), indent=2)}\n\n",
        ]

        # Include file summaries for reference
        file_summaries = deep_dive.get("file_summaries", {})
        if file_summaries:
            context_parts.append("## File Reference (for issue descriptions)\n")
            for path, summary in file_summaries.items():
                context_parts.append(
                    f"- `{path}`: {summary.get('purpose', '?')} [{summary.get('status', '?')}]\n"
                )

        # Include existing issues for dedup
        issues = existing.get("existing_issues", [])
        if issues:
            context_parts.append(f"\n## Existing Issues (DO NOT duplicate)\n")
            for issue in issues:
                labels = ", ".join(l.get("name", "") for l in issue.get("labels", []))
                context_parts.append(
                    f"- #{issue.get('number', '?')} {issue.get('title', '?')} [{labels}]\n"
                )

        context = "".join(context_parts)
        log.info("pass4_context_size", chars=len(context))

        final_text, tool_call_log = await self._run_pass(
            "pass4",
            self._pass_prompts["pass4_issues"],
            context,
            tools=self._issue_tools,
            max_calls=80,
            model=settings.ai_model_onboarding_pass4,
        )

        log.info("pass4_complete", tool_calls=len(tool_call_log))
        return final_text, tool_call_log

    # ── Main Handle ────────────────────────────────────────────────────

    async def handle(self, context: AgentContext) -> AgentResult:
        log.info(
            "onboarding_start",
            repo=self.repo_full_name,
            webhook_event=context.event_type,
        )

        scratchpad: dict[str, Any] = {}
        all_tool_calls: list[dict] = []
        status = "success"

        # Pass 1: Structure
        try:
            scratchpad["structure"] = await self._pass1_structure()
        except Exception as e:
            log.error("pass1_failed", error=str(e))
            return AgentResult(
                agent_name=self.name,
                status="failed",
                data={"error": f"Pass 1 (Structure) failed: {e}"},
            )

        # Pass 2: Deep Dive
        try:
            scratchpad["deep_dive"] = await self._pass2_deep_dive(scratchpad)
        except Exception as e:
            log.error("pass2_failed", error=str(e))
            # Fall back: use Pass 1 data only for milestones
            scratchpad["deep_dive"] = {
                "file_summaries": {},
                "features_found": [],
                "gaps_found": [{"area": "Deep dive failed", "severity": "high", "details": str(e)}],
                "tech_details": scratchpad["structure"].get("stack", {}),
            }
            status = "partial"

        # Pass 2.5: Fetch existing state
        try:
            scratchpad["existing"] = await self._pass2_5_reconciliation()
        except Exception as e:
            log.error("pass2_5_failed", error=str(e))
            scratchpad["existing"] = {"existing_issues": [], "collaborators": []}

        # Pass 3: Milestones
        try:
            scratchpad["milestones"] = await self._pass3_milestones(scratchpad)
        except Exception as e:
            log.error("pass3_failed", error=str(e))
            return AgentResult(
                agent_name=self.name,
                status="failed",
                data={"error": f"Pass 3 (Milestones) failed: {e}", "scratchpad": scratchpad},
            )

        # Pass 3.5: Validate plan
        try:
            scratchpad["milestones"] = await self._pass3_5_validate(scratchpad)
        except Exception as e:
            log.error("pass3_5_failed", error=str(e))
            # Non-fatal: proceed with unvalidated plan

        # Pass 4: Create issues
        try:
            final_text, tool_call_log = await self._pass4_issues(scratchpad)
            all_tool_calls.extend(tool_call_log)
        except Exception as e:
            log.error("pass4_failed", error=str(e))
            status = "partial"
            final_text = f"Pass 4 failed: {e}"
            tool_call_log = []

        # Persist onboarding run
        try:
            issues_created = sum(
                1 for tc in all_tool_calls
                if tc["tool"] == "create_issue" and tc["result"]["success"]
            )
            await _save_onboarding_run(
                repo_id=self.repo_id,
                status=status,
                repo_snapshot=_strip_tree(scratchpad),
                suggested_plan=scratchpad.get("milestones", {}),
                existing_state=scratchpad.get("existing", {}),
                actions_taken=[
                    {"tool": tc["tool"], "success": tc["result"]["success"]}
                    for tc in all_tool_calls
                ],
                issues_created=issues_created,
                confidence=scratchpad.get("milestones", {}).get("overall_confidence", 0.0),
            )
        except Exception as e:
            log.error("save_onboarding_run_failed", error=str(e))

        log.info(
            "onboarding_complete",
            repo=self.repo_full_name,
            status=status,
            total_tool_calls=len(all_tool_calls),
        )

        return AgentResult(
            agent_name=self.name,
            status=status,
            actions_taken=[
                {"tool": tc["tool"], "success": tc["result"]["success"]}
                for tc in all_tool_calls
            ],
            data={
                "final_response": final_text,
                "milestones_planned": len(scratchpad.get("milestones", {}).get("milestones", [])),
                "issues_created": sum(
                    1 for tc in all_tool_calls
                    if tc["tool"] == "create_issue" and tc["result"]["success"]
                ),
            },
            confidence=scratchpad.get("milestones", {}).get("overall_confidence", 0.7),
            should_notify=True,
            comment_body=self._build_summary_comment(scratchpad, all_tool_calls),
        )

    def _build_summary_comment(self, scratchpad: dict, tool_call_log: list[dict]) -> str:
        tracker_issues = sum(
            1 for tc in tool_call_log
            if tc["tool"] == "create_issue"
            and "Milestone Tracker" in str(tc.get("args", {}).get("labels", []))
        )
        sub_issues = sum(
            1 for tc in tool_call_log
            if tc["tool"] == "create_issue"
        ) - tracker_issues

        project_name = scratchpad.get("structure", {}).get("project_name", self.repo_full_name)
        milestones_planned = len(scratchpad.get("milestones", {}).get("milestones", []))

        lines = [
            "## Onboarding Complete",
            "",
            f"I've analyzed **{project_name}** using a multi-pass scan and set up project tracking.",
            "",
            "### Analysis Summary",
            f"- Features identified: **{len(scratchpad.get('deep_dive', {}).get('features_found', []))}**",
            f"- Gaps identified: **{len(scratchpad.get('deep_dive', {}).get('gaps_found', []))}**",
            "",
            "### Issues Created",
            f"- Milestones planned: **{milestones_planned}**",
            f"- Milestone Tracker issues: **{max(tracker_issues, 0)}**",
            f"- Sub-issues: **{max(sub_issues, 0)}**",
            "",
            "---",
            "*Generated by GitHub Assistant — Onboarding Agent*",
        ]
        return "\n".join(lines)


def _strip_tree(scratchpad: dict) -> dict:
    """Remove the raw tree from scratchpad before persisting (it's large and redundant)."""
    result = dict(scratchpad)
    if "structure" in result:
        structure = dict(result["structure"])
        structure.pop("_tree", None)
        result["structure"] = structure
    return result
