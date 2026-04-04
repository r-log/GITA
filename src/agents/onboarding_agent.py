"""
Onboarding Agent -- project setup and progressive tracking.

Two flows handled by the same agent:

FRESH (no existing Milestone Trackers):
  Step 1: Index -- deterministic code parsing, zero LLM cost
  Step 2: Fetch State -- existing issues + collaborators (no LLM)
  Step 3: Milestones -- LLM reads code map -> milestone plan
  Step 3.5: Validation -- deterministic checks + optional LLM spot-check
  Step 4: Issues -- LLM creates sub-issues + Milestone Tracker issues

PROGRESSIVE (existing Milestone Trackers found):
  Step 1: Index -- same
  Step 2: Fetch State -- same, detects is_progressive
  Step 3P: Progressive Analysis -- LLM compares code map vs existing issues -> action list
  Step 4P: Execute Actions -- deterministic, no LLM cost
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

# Indexer
from src.indexer.indexer import index_repository

# GitHub tools
from src.tools.github.repos import _get_collaborators
from src.tools.github.issues import make_create_issue, make_update_issue, _get_all_issues, _create_issue, _update_issue
from src.tools.github.labels import make_add_label, make_create_label
from src.tools.github.comments import make_post_comment, _post_comment
from src.utils.checklist import parse_checklist, add_checklist_items

# AI tools
from src.tools.ai.project_planner import make_compare_plan_vs_state

# DB tools
from src.tools.db.onboarding import make_save_onboarding_run, make_save_file_mapping, _save_onboarding_run
from src.tools.db.code_index import make_query_code_index, make_save_issue_record

log = structlog.get_logger()


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
    Hybrid onboarding agent. Step 1 is fully deterministic (code indexer),
    Steps 3-4 use LLM for reasoning and issue creation.
    """

    def __init__(self, installation_id: int, repo_full_name: str, repo_id: int = 0, model: str | None = None):
        # Validation tools (query code index instead of reading files from GitHub)
        self._validation_tools = [
            make_query_code_index(repo_id),
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
            make_save_issue_record(repo_id),
        ]

        # Initialize with all tools (base class needs them for registration)
        all_tools = self._validation_tools + self._plan_tools + self._issue_tools
        seen = set()
        unique_tools = []
        for t in all_tools:
            if t.name not in seen:
                seen.add(t.name)
                unique_tools.append(t)

        super().__init__(
            name="onboarding",
            description="Project setup specialist -- scans repos, creates Milestone Tracker issues with linked sub-issues",
            tools=unique_tools,
            system_prompt_file="onboarding.md",
        )

        self.installation_id = installation_id
        self.repo_full_name = repo_full_name
        self.repo_id = repo_id

        # Load per-pass prompts (only the passes that still use LLM)
        self._pass_prompts: dict[str, str] = {}
        for pass_name in ["pass3_milestones", "pass3_5_validation", "pass4_issues", "pass3_progressive"]:
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
        # Track token usage per model
        if response.usage:
            pt = response.usage.prompt_tokens or 0
            ct = response.usage.completion_tokens or 0
            self._usage["prompt_tokens"] += pt
            self._usage["completion_tokens"] += ct
            self._usage["llm_calls"] += 1
            if use_model not in self._usage["by_model"]:
                self._usage["by_model"][use_model] = {"prompt_tokens": 0, "completion_tokens": 0}
            self._usage["by_model"][use_model]["prompt_tokens"] += pt
            self._usage["by_model"][use_model]["completion_tokens"] += ct

        raw = response.choices[0].message.content or ""
        return _extract_json(raw)

    # -- Step 1: Index (deterministic, zero LLM cost) ---------------------

    async def _step1_index(self) -> str:
        """
        Download all files, parse deterministically with AST/regex,
        store in code_index DB, generate compressed code map.
        Returns the code map text (~2-10KB).
        """
        log.info("step1_index_start", repo=self.repo_full_name)

        code_map = await index_repository(
            installation_id=self.installation_id,
            repo_full_name=self.repo_full_name,
            repo_id=self.repo_id,
        )

        log.info("step1_index_complete", repo=self.repo_full_name, code_map_size=len(code_map))
        return code_map

    # -- Step 2: Fetch Existing State (no LLM) ----------------------------

    async def _step2_fetch_state(self) -> dict[str, Any]:
        """
        Fetch existing issues and collaborators. No LLM call -- pure data fetch.
        Detects if this is a progressive run (existing Milestone Trackers found).
        """
        log.info("step2_fetch_state_start", repo=self.repo_full_name)

        issues_result, collab_result = await asyncio.gather(
            _get_all_issues(self.installation_id, self.repo_full_name),
            _get_collaborators(self.installation_id, self.repo_full_name),
        )

        existing_issues = issues_result.data if issues_result.success else []
        collaborators = collab_result.data if collab_result.success else []

        # Detect progressive mode: do any issues have the Milestone Tracker label?
        milestone_trackers = [
            i for i in existing_issues
            if any(l.get("name") == "Milestone Tracker" for l in i.get("labels", []))
        ]
        is_progressive = len(milestone_trackers) > 0

        log.info(
            "step2_fetch_state_complete",
            issues=len(existing_issues),
            collaborators=len(collaborators),
            milestone_trackers=len(milestone_trackers),
            is_progressive=is_progressive,
        )

        return {
            "existing_issues": existing_issues,
            "collaborators": collaborators,
            "milestone_trackers": milestone_trackers,
            "is_progressive": is_progressive,
        }

    # -- Step 3: Milestones (LLM reads code map) --------------------------

    async def _step3_milestones(self, scratchpad: dict) -> dict[str, Any]:
        """
        LLM reads the code map (~2-10KB) and proposes milestones.
        Single LLM call, no tools needed. Much cheaper than old Pass 1+2+3.
        """
        log.info("step3_start")

        code_map = scratchpad["code_map"]
        existing = scratchpad["existing"]

        # Build context: code map + existing issues
        context_parts = [
            f"# Repository: {self.repo_full_name}\n\n",
            "## Code Map (deterministic analysis)\n\n",
            code_map,
            "\n\n",
        ]

        # Existing issues for reconciliation
        issues = existing.get("existing_issues", [])
        if issues:
            context_parts.append(f"## Existing Issues ({len(issues)} open)\n")
            for issue in issues:
                labels = ", ".join(l.get("name", "") for l in issue.get("labels", []))
                context_parts.append(
                    f"- #{issue.get('number', '?')} {issue.get('title', '?')} [{labels}]\n"
                )

        context = "".join(context_parts)
        log.info("step3_context_size", chars=len(context))

        raw = await self._llm_call(
            self._pass_prompts["pass3_milestones"],
            context,
            model=settings.ai_model_onboarding_pass3,
        )

        try:
            result = json.loads(_extract_json(raw))
        except json.JSONDecodeError:
            log.error("step3_json_parse_failed", raw=raw[:500])
            raise RuntimeError("Step 3 failed: LLM returned invalid JSON")

        log.info(
            "step3_complete",
            milestones=len(result.get("milestones", [])),
            confidence=result.get("overall_confidence"),
        )
        return result

    # -- Step 3.5: Validate Plan -------------------------------------------

    async def _step3_5_validate(self, scratchpad: dict) -> dict[str, Any]:
        """
        Validate milestone plan before issue creation.
        Stage A: deterministic checks (fuzzy dedup against existing issues).
        Stage B: LLM spot-check of ambiguous items using query_code_index.
        """
        log.info("step3_5_start")

        milestones_data = scratchpad["milestones"]
        existing_issues = scratchpad["existing"].get("existing_issues", [])

        # Stage A: Deterministic checks
        flags: list[dict] = []
        auto_skipped = 0
        auto_corrected = 0

        for milestone in milestones_data.get("milestones", []):
            for task in milestone.get("tasks", []):
                task_title = task.get("title", "")

                # Fuzzy dedup against existing issues
                if existing_issues:
                    best_score = 0
                    best_match = None
                    for issue in existing_issues:
                        score = fuzz.ratio(task_title.lower(), issue.get("title", "").lower())
                        if score > best_score:
                            best_score = score
                            best_match = issue

                    if best_score >= 80:
                        # Clear duplicate -- auto-skip
                        task["_validation"] = "skip"
                        task["_skip_reason"] = f"Duplicate of #{best_match['number']}: {best_match['title']} (score={best_score})"
                        auto_skipped += 1
                        log.info("step3_5_auto_skip", task=task_title, duplicate_of=best_match["number"], score=best_score)
                    elif best_score >= 50:
                        flags.append({
                            "milestone_title": milestone.get("title", ""),
                            "task_title": task_title,
                            "flag_type": "possible_duplicate",
                            "details": f"Similar to #{best_match['number']}: {best_match['title']} (score={best_score})",
                            "existing_issue": {"number": best_match["number"], "title": best_match["title"]},
                        })

                # Check status vs referenced files (query code_index instead of tree)
                task_files = task.get("files", [])
                task_status = task.get("status", "not-started")
                if task_files and task_status == "not-started":
                    flags.append({
                        "milestone_title": milestone.get("title", ""),
                        "task_title": task_title,
                        "flag_type": "status_check",
                        "details": f"Task references files: {task_files}. Verify via code index if they exist and are implemented.",
                        "files_to_check": task_files[:3],
                    })

        log.info("step3_5_stage_a_complete", flags=len(flags), auto_skipped=auto_skipped)

        # Stage B: LLM spot-check if there are flagged items
        if flags:
            context = json.dumps({
                "flagged_items": flags,
                "project_name": self.repo_full_name,
            }, indent=2)

            raw, tool_call_log = await self._run_pass(
                "pass3_5",
                self._pass_prompts["pass3_5_validation"],
                context,
                tools=self._validation_tools,
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
                                log.info("step3_5_status_corrected", task=d_task, old=old_status, new=task["status"])

                log.info("step3_5_llm_decisions", decisions=len(decisions))
            except json.JSONDecodeError:
                log.error("step3_5_json_parse_failed", raw=raw[:500])

        # Remove skipped tasks from milestones
        for milestone in milestones_data.get("milestones", []):
            original_count = len(milestone.get("tasks", []))
            milestone["tasks"] = [
                t for t in milestone.get("tasks", [])
                if t.get("_validation") != "skip"
            ]
            removed = original_count - len(milestone["tasks"])
            if removed:
                log.info("step3_5_tasks_removed", milestone=milestone.get("title"), removed=removed)

        # Remove empty milestones (all tasks skipped)
        original_milestone_count = len(milestones_data.get("milestones", []))
        milestones_data["milestones"] = [
            m for m in milestones_data.get("milestones", [])
            if m.get("tasks")
        ]
        removed_milestones = original_milestone_count - len(milestones_data["milestones"])

        log.info(
            "step3_5_complete",
            tasks_skipped=auto_skipped,
            tasks_corrected=auto_corrected,
            milestones_removed=removed_milestones,
            milestones_remaining=len(milestones_data["milestones"]),
        )

        return milestones_data

    # -- Step 4: Create Issues ---------------------------------------------

    async def _step4_issues(self, scratchpad: dict) -> tuple[str, list[dict]]:
        """
        Create sub-issues and Milestone Tracker issues using the tool loop.
        Now also calls save_issue_record to persist each created issue in the DB.
        """
        log.info("step4_start")

        milestones = scratchpad["milestones"]
        existing = scratchpad["existing"]

        # Build context for the issue-creation LLM
        context_parts = [
            f"# Milestone Plan for {self.repo_full_name}\n\n",
            f"## Project Summary\n{milestones.get('project_summary', 'N/A')}\n\n",
            f"## Milestones to Create\n\n{json.dumps(milestones.get('milestones', []), indent=2)}\n\n",
        ]

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
        log.info("step4_context_size", chars=len(context))

        final_text, tool_call_log = await self._run_pass(
            "pass4",
            self._pass_prompts["pass4_issues"],
            context,
            tools=self._issue_tools,
            max_calls=80,
            model=settings.ai_model_onboarding_pass4,
        )

        log.info("step4_complete", tool_calls=len(tool_call_log))
        return final_text, tool_call_log

    # -- Progressive Flow ---------------------------------------------------

    def _build_progressive_context(self, scratchpad: dict) -> str:
        """
        Build enriched context for the progressive LLM call:
        code map + existing Milestone Trackers with their sub-issue states.
        """
        code_map = scratchpad["code_map"]
        existing = scratchpad["existing"]
        all_issues = existing.get("existing_issues", [])
        trackers = existing.get("milestone_trackers", [])

        # Index all issues by number for quick lookup
        issue_by_number = {i["number"]: i for i in all_issues}

        parts = [
            f"# Repository: {self.repo_full_name}\n\n",
            "## Code Map (deterministic analysis)\n\n",
            code_map,
            "\n\n## Existing Milestone Trackers\n\n",
        ]

        for tracker in trackers:
            t_number = tracker.get("number", "?")
            t_title = tracker.get("title", "?")
            t_state = tracker.get("state", "open")
            t_body = tracker.get("body", "")

            parts.append(f"### Milestone Tracker #{t_number}: {t_title} [{t_state}]\n")

            # Parse the checklist from the tracker body
            checklist = parse_checklist(t_body)
            if checklist:
                for item in checklist:
                    check = "x" if item["checked"] else " "
                    issue_num = item.get("issue_number")
                    if issue_num and issue_num in issue_by_number:
                        sub = issue_by_number[issue_num]
                        sub_state = sub.get("state", "?")
                        sub_labels = ", ".join(l.get("name", "") for l in sub.get("labels", []))
                        parts.append(f"- [{check}] {item['text']} (#{issue_num}) -- {sub_state} [{sub_labels}]\n")
                    else:
                        parts.append(f"- [{check}] {item['text']}")
                        if issue_num:
                            parts.append(f" (#{issue_num})")
                        parts.append("\n")
            else:
                parts.append("(no checklist found in body)\n")
            parts.append("\n")

        # Orphan issues: open issues not linked to any tracker
        linked_numbers = set()
        for tracker in trackers:
            body = tracker.get("body", "")
            for item in parse_checklist(body):
                if item.get("issue_number"):
                    linked_numbers.add(item["issue_number"])
        tracker_numbers = {t["number"] for t in trackers}

        orphans = [
            i for i in all_issues
            if i["number"] not in linked_numbers
            and i["number"] not in tracker_numbers
            and i.get("state") == "open"
        ]
        if orphans:
            parts.append("## Untracked Open Issues\n")
            for orphan in orphans:
                labels = ", ".join(l.get("name", "") for l in orphan.get("labels", []))
                parts.append(f"- #{orphan['number']} {orphan.get('title', '?')} [{labels}]\n")

        context = "".join(parts)

        # Safety: truncate if too large (keep code map + most recent trackers)
        if len(context) > 30000:
            log.warning("progressive_context_truncated", original_size=len(context))
            context = context[:30000] + "\n\n... [truncated]"

        return context

    async def _step3_progressive(self, scratchpad: dict) -> dict[str, Any]:
        """
        Progressive analysis: single LLM call comparing code map vs existing issues.
        Returns an action list (close, create, update, flag).
        """
        log.info("step3_progressive_start")

        context = self._build_progressive_context(scratchpad)
        log.info("step3_progressive_context_size", chars=len(context))

        raw = await self._llm_call(
            self._pass_prompts["pass3_progressive"],
            context,
            model=settings.ai_model_onboarding_pass3_progressive,
        )

        try:
            result = json.loads(_extract_json(raw))
        except json.JSONDecodeError:
            log.error("step3_progressive_json_parse_failed", raw=raw[:500])
            raise RuntimeError("Step 3P failed: LLM returned invalid JSON")

        actions = result.get("actions", [])
        log.info(
            "step3_progressive_complete",
            actions=len(actions),
            health=result.get("analysis", {}).get("overall_health"),
            confidence=result.get("overall_confidence"),
        )
        return result

    async def _step4_progressive_execute(self, scratchpad: dict) -> tuple[str, list[dict]]:
        """
        Execute progressive actions deterministically. No LLM cost.
        Processes actions in order: create_milestone, create_issue, close_issue,
        update_tracker, flag_stale.
        """
        log.info("step4_progressive_start")

        progressive = scratchpad["progressive"]
        actions = progressive.get("actions", [])
        tool_call_log: list[dict] = []

        # Sort actions: create first (we need issue numbers), then close/update/flag
        creates_milestone = [a for a in actions if a["type"] == "create_milestone"]
        creates_issue = [a for a in actions if a["type"] == "create_issue"]
        closes = [a for a in actions if a["type"] == "close_issue"]
        updates_tracker = [a for a in actions if a["type"] == "update_tracker"]
        flags = [a for a in actions if a["type"] == "flag_stale"]

        # Track new issue numbers for tracker updates
        new_issue_numbers: dict[str, int] = {}  # title -> github number

        # 1. Create new milestones (sub-issues + tracker)
        for ms in creates_milestone:
            log.info("progressive_create_milestone", title=ms.get("title"))
            task_numbers = []

            for task in ms.get("tasks", []):
                result = await _create_issue(
                    self.installation_id, self.repo_full_name,
                    title=task["title"],
                    body=task.get("description", ""),
                    labels=task.get("labels", ["enhancement"]),
                )
                if result.success:
                    num = result.data.get("number", 0)
                    task_numbers.append((task["title"], num))
                    new_issue_numbers[task["title"]] = num
                    tool_call_log.append({"tool": "create_issue", "result": {"success": True}, "args": {"title": task["title"]}})
                    log.info("progressive_issue_created", title=task["title"], number=num)

            # Create the tracker issue
            checklist = "\n".join(
                f"- [ ] {title} (#{num})" for title, num in task_numbers
            )
            tracker_body = f"## {ms.get('description', ms.get('title', ''))}\n\n**Deadline:** TBD\n\n### Tasks\n{checklist}"
            result = await _create_issue(
                self.installation_id, self.repo_full_name,
                title=ms["title"],
                body=tracker_body,
                labels=["Milestone Tracker"],
            )
            if result.success:
                tool_call_log.append({"tool": "create_issue", "result": {"success": True}, "args": {"title": ms["title"], "labels": ["Milestone Tracker"]}})
                log.info("progressive_tracker_created", title=ms["title"], number=result.data.get("number"))

        # 2. Create new sub-issues under existing trackers
        for action in creates_issue:
            result = await _create_issue(
                self.installation_id, self.repo_full_name,
                title=action["title"],
                body=action.get("description", ""),
                labels=action.get("labels", ["enhancement"]),
            )
            if result.success:
                num = result.data.get("number", 0)
                new_issue_numbers[action["title"]] = num
                tool_call_log.append({"tool": "create_issue", "result": {"success": True}, "args": {"title": action["title"]}})
                log.info("progressive_issue_created", title=action["title"], number=num)

        # 3. Close completed issues
        for action in closes:
            issue_num = action.get("issue_number")
            reason = action.get("reason", "Completed based on code analysis")

            # Post a comment explaining the closure
            await _post_comment(
                self.installation_id, self.repo_full_name, issue_num,
                f"Closing: {reason}\n\n*-- GITA Progressive Update*",
            )
            result = await _update_issue(
                self.installation_id, self.repo_full_name, issue_num,
                state="closed",
            )
            if result.success:
                tool_call_log.append({"tool": "close_issue", "result": {"success": True}, "args": {"issue_number": issue_num}})
                log.info("progressive_issue_closed", number=issue_num, reason=reason)

        # 4. Update tracker checklists (add new task lines)
        for action in updates_tracker:
            tracker_num = action.get("issue_number")
            add_tasks = action.get("add_tasks", [])
            if not add_tasks or not tracker_num:
                continue

            # Resolve task titles to real issue numbers
            checklist_lines = []
            for task_text in add_tasks:
                # Find the matching created issue number
                matched_num = new_issue_numbers.get(task_text)
                if matched_num:
                    checklist_lines.append(f"- [ ] {task_text} (#{matched_num})")
                else:
                    checklist_lines.append(f"- [ ] {task_text}")

            # Fetch current tracker body and append new items
            existing = scratchpad["existing"]
            tracker_issue = next(
                (i for i in existing.get("existing_issues", []) if i["number"] == tracker_num),
                None,
            )
            if tracker_issue:
                current_body = tracker_issue.get("body", "")
                updated_body = add_checklist_items(current_body, checklist_lines)
                result = await _update_issue(
                    self.installation_id, self.repo_full_name, tracker_num,
                    body=updated_body,
                )
                if result.success:
                    tool_call_log.append({"tool": "update_tracker", "result": {"success": True}, "args": {"issue_number": tracker_num}})
                    log.info("progressive_tracker_updated", number=tracker_num, added=len(checklist_lines))

        # 5. Flag stale issues
        for action in flags:
            issue_num = action.get("issue_number")
            reason = action.get("reason", "May be outdated")
            await _post_comment(
                self.installation_id, self.repo_full_name, issue_num,
                f"**Stale check:** {reason}\n\nPlease review if this issue is still relevant.\n\n*-- GITA Progressive Update*",
            )
            tool_call_log.append({"tool": "flag_stale", "result": {"success": True}, "args": {"issue_number": issue_num}})
            log.info("progressive_issue_flagged", number=issue_num, reason=reason)

        summary = progressive.get("summary", "Progressive update complete")
        log.info("step4_progressive_complete", tool_calls=len(tool_call_log))
        return summary, tool_call_log

    # -- Main Handle -------------------------------------------------------

    async def handle(self, context: AgentContext) -> AgentResult:
        log.info(
            "onboarding_start",
            repo=self.repo_full_name,
            webhook_event=context.event_type,
        )

        scratchpad: dict[str, Any] = {}
        all_tool_calls: list[dict] = []
        status = "success"

        # Step 1: Index (deterministic -- zero LLM cost)
        try:
            scratchpad["code_map"] = await self._step1_index()
        except Exception as e:
            log.error("step1_failed", error=str(e))
            return AgentResult(
                agent_name=self.name,
                status="failed",
                data={"error": f"Step 1 (Index) failed: {e}"},
            )

        # Step 2: Fetch existing state (no LLM, detects progressive mode)
        try:
            scratchpad["existing"] = await self._step2_fetch_state()
        except Exception as e:
            log.error("step2_failed", error=str(e))
            scratchpad["existing"] = {"existing_issues": [], "collaborators": [], "is_progressive": False, "milestone_trackers": []}

        is_progressive = scratchpad["existing"].get("is_progressive", False)
        log.info("onboarding_flow", flow="progressive" if is_progressive else "fresh")

        if is_progressive:
            # ── Progressive Flow ──────────────────────────────────
            # Step 3P: LLM compares code map vs existing issues
            try:
                scratchpad["progressive"] = await self._step3_progressive(scratchpad)
            except Exception as e:
                log.error("step3_progressive_failed", error=str(e))
                return AgentResult(
                    agent_name=self.name,
                    status="failed",
                    data={"error": f"Step 3P (Progressive) failed: {e}"},
                )

            # Step 4P: Execute actions deterministically ($0)
            try:
                final_text, tool_call_log = await self._step4_progressive_execute(scratchpad)
                all_tool_calls.extend(tool_call_log)
            except Exception as e:
                log.error("step4_progressive_failed", error=str(e))
                status = "partial"
                final_text = f"Step 4P failed: {e}"

            run_status = "progressive_update"
            confidence = scratchpad.get("progressive", {}).get("overall_confidence", 0.7)
            plan_data = scratchpad.get("progressive", {})

        else:
            # ── Fresh Flow ────────────────────────────────────────
            # Step 3: Milestones (single LLM call with code map)
            try:
                scratchpad["milestones"] = await self._step3_milestones(scratchpad)
            except Exception as e:
                log.error("step3_failed", error=str(e))
                return AgentResult(
                    agent_name=self.name,
                    status="failed",
                    data={"error": f"Step 3 (Milestones) failed: {e}"},
                )

            # Step 3.5: Validate plan
            try:
                scratchpad["milestones"] = await self._step3_5_validate(scratchpad)
            except Exception as e:
                log.error("step3_5_failed", error=str(e))

            # Step 4: Create issues (LLM tool loop)
            try:
                final_text, tool_call_log = await self._step4_issues(scratchpad)
                all_tool_calls.extend(tool_call_log)
            except Exception as e:
                log.error("step4_failed", error=str(e))
                status = "partial"
                final_text = f"Step 4 failed: {e}"

            run_status = status
            confidence = scratchpad.get("milestones", {}).get("overall_confidence", 0.0)
            plan_data = scratchpad.get("milestones", {})

        # Persist onboarding run
        try:
            issues_created = sum(
                1 for tc in all_tool_calls
                if tc["tool"] == "create_issue" and tc["result"]["success"]
            )
            await _save_onboarding_run(
                repo_id=self.repo_id,
                status=run_status,
                repo_snapshot={"code_map_size": len(scratchpad.get("code_map", ""))},
                suggested_plan=plan_data,
                existing_state=scratchpad.get("existing", {}),
                actions_taken=[
                    {"tool": tc["tool"], "success": tc["result"]["success"]}
                    for tc in all_tool_calls
                ],
                issues_created=issues_created,
                confidence=confidence,
            )
        except Exception as e:
            log.error("save_onboarding_run_failed", error=str(e))

        log.info(
            "onboarding_complete",
            repo=self.repo_full_name,
            status=run_status,
            flow="progressive" if is_progressive else "fresh",
            total_tool_calls=len(all_tool_calls),
        )

        return AgentResult(
            agent_name=self.name,
            status=run_status,
            actions_taken=[
                {"tool": tc["tool"], "success": tc["result"]["success"]}
                for tc in all_tool_calls
            ],
            data={
                "final_response": final_text,
                "flow": "progressive" if is_progressive else "fresh",
                "issues_created": issues_created,
                "usage": dict(self._usage),
            },
            confidence=confidence,
            should_notify=True,
            comment_body=self._build_summary_comment(scratchpad, all_tool_calls, is_progressive),
        )

    def _build_summary_comment(self, scratchpad: dict, tool_call_log: list[dict], is_progressive: bool = False) -> str:
        if is_progressive:
            return self._build_progressive_summary(scratchpad, tool_call_log)
        return self._build_fresh_summary(scratchpad, tool_call_log)

    def _build_fresh_summary(self, scratchpad: dict, tool_call_log: list[dict]) -> str:
        tracker_issues = sum(
            1 for tc in tool_call_log
            if tc["tool"] == "create_issue"
            and "Milestone Tracker" in str(tc.get("args", {}).get("labels", []))
        )
        sub_issues = sum(
            1 for tc in tool_call_log
            if tc["tool"] == "create_issue"
        ) - tracker_issues

        milestones_planned = len(scratchpad.get("milestones", {}).get("milestones", []))

        lines = [
            "## Onboarding Complete",
            "",
            f"I've analyzed **{self.repo_full_name}** using deterministic code indexing and set up project tracking.",
            "",
            "### Analysis Summary",
            "- Code map generated from deterministic parsing (zero LLM cost)",
            "- Code index stored in database for future agent queries",
            "",
            "### Issues Created",
            f"- Milestones planned: **{milestones_planned}**",
            f"- Milestone Tracker issues: **{max(tracker_issues, 0)}**",
            f"- Sub-issues: **{max(sub_issues, 0)}**",
            "",
            "---",
            "*Generated by GITA -- Onboarding Agent*",
        ]
        return "\n".join(lines)

    def _build_progressive_summary(self, scratchpad: dict, tool_call_log: list[dict]) -> str:
        progressive = scratchpad.get("progressive", {})
        analysis = progressive.get("analysis", {})
        health = analysis.get("overall_health", "unknown")

        created = sum(1 for tc in tool_call_log if tc["tool"] == "create_issue")
        closed = sum(1 for tc in tool_call_log if tc["tool"] == "close_issue")
        updated = sum(1 for tc in tool_call_log if tc["tool"] == "update_tracker")
        flagged = sum(1 for tc in tool_call_log if tc["tool"] == "flag_stale")

        lines = [
            "## Progressive Update Complete",
            "",
            f"I've compared the current codebase of **{self.repo_full_name}** against existing Milestone Trackers.",
            "",
            f"**Project health:** {health}",
            "",
            "### Actions Taken",
        ]

        if created:
            lines.append(f"- Issues created: **{created}**")
        if closed:
            lines.append(f"- Issues closed (completed): **{closed}**")
        if updated:
            lines.append(f"- Milestone Trackers updated: **{updated}**")
        if flagged:
            lines.append(f"- Issues flagged for review: **{flagged}**")
        if not any([created, closed, updated, flagged]):
            lines.append("- No changes needed -- tracking is up to date")

        completed = analysis.get("completed_since_last_run", [])
        if completed:
            lines.append("")
            lines.append("### What changed")
            for item in completed[:5]:
                lines.append(f"- {item}")

        lines.extend(["", "---", "*Generated by GITA -- Progressive Update*"])
        return "\n".join(lines)
