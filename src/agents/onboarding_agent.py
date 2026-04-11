"""
Onboarding Agent -- project setup and progressive tracking.

Two flows handled by the same agent:

FRESH (no existing Milestone Trackers):
  Step 1: Index -- deterministic code parsing, zero LLM cost
  Step 2: Fetch State -- existing issues + collaborators (no LLM)
  Step 3: Milestones -- LLM reads code map -> milestone plan
  Step 3.5: Validation -- deterministic checks + optional LLM spot-check
  Step 4: Issues -- deterministic issue creation from plan (no LLM cost)

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
from src.tools.github.issues import _get_all_issues, _create_issue, _update_issue
from src.tools.github.labels import _create_label
from src.tools.github.comments import _post_comment
from src.utils.checklist import parse_checklist, add_checklist_items

# DB tools
from src.tools.db.onboarding import _save_onboarding_run
from src.tools.db.code_index import make_query_code_index, _save_issue_record, _query_code_index
from src.tools.db.code_retrieval import (
    make_list_project_files,
    make_get_function_code,
    make_get_class_code,
    make_get_code_slice,
    make_read_file,
    make_search_in_file,
)
from src.tools.onboarding.scratchpad_tools import (
    make_record_finding,
    make_finalize_exploration,
)

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
        # Validation tools (only pass that still uses an LLM tool loop)
        self._validation_tools = [
            make_query_code_index(repo_id),
        ]

        super().__init__(
            name="onboarding",
            description="Project setup specialist -- scans repos, creates Milestone Tracker issues with linked sub-issues",
            tools=self._validation_tools,
            system_prompt_file="onboarding.md",
        )

        self.installation_id = installation_id
        self.repo_full_name = repo_full_name
        self.repo_id = repo_id

        # Load per-pass prompts (only the passes that still use LLM)
        self._pass_prompts: dict[str, str] = {}
        pass_names = [
            "pass3_milestones",          # legacy fallback
            "pass3_5_validation",
            "pass3_progressive",         # legacy progressive fallback
            "pass3a_explore",            # new agentic review loop
            "pass3b_audit",              # new finding auditor
            "pass3c_group",              # new findings → milestones grouper
            "pass3c_group_progressive",  # new findings → progressive actions grouper
        ]
        for pass_name in pass_names:
            prompt_path = Path("prompts") / f"onboarding_{pass_name}.md"
            if prompt_path.exists():
                self._pass_prompts[pass_name] = prompt_path.read_text(encoding="utf-8")
            else:
                # New prompts are optional during the rollout — if missing,
                # the legacy path still works.
                if pass_name.startswith(("pass3a_", "pass3b_", "pass3c_")):
                    log.warning("onboarding_prompt_missing", pass_name=pass_name)
                    continue
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

    # -- Step 3: Milestones ------------------------------------------------
    #
    # The new agentic review loop replaces the single-call "LLM reads code map"
    # approach. Three sub-steps:
    #   3a. Explorer — BaseAgent tool loop, LLM pulls code slices and records findings
    #   3b. Auditor — drops generic / unverifiable findings
    #   3c. Grouper — turns findings into the existing milestones JSON schema
    #
    # The legacy single-call path is kept as `_step3_milestones_legacy` and runs
    # automatically if the new flow throws or if the feature flag is disabled.

    async def _step3_milestones(self, scratchpad: dict) -> dict[str, Any]:
        use_new = getattr(settings, "onboarding_use_agentic_review", True)
        if not use_new or not all(
            self._pass_prompts.get(k)
            for k in ("pass3a_explore", "pass3b_audit", "pass3c_group")
        ):
            log.info("step3_using_legacy_flow", reason="flag_disabled_or_prompts_missing")
            return await self._step3_milestones_legacy(scratchpad)

        try:
            scratchpad["findings"] = []
            await self._step3a_explore(scratchpad)
            kept_findings = await self._step3b_audit(scratchpad)
            scratchpad["findings"] = kept_findings
            return await self._step3c_group(scratchpad)
        except Exception as e:
            log.warning(
                "step3_new_flow_failed",
                error=str(e),
                findings_so_far=len(scratchpad.get("findings", [])),
                exc_info=True,
            )
            # Fallback: run the legacy single-call flow so onboarding never fully fails.
            return await self._step3_milestones_legacy(scratchpad)

    # ── Step 3a: Explorer (agentic tool loop) ─────────────────────────────

    async def _step3a_explore(self, scratchpad: dict) -> None:
        """
        Run a BaseAgent-style tool loop that lets the LLM read real code and
        record findings. Populates scratchpad["findings"] and scratchpad["project_summary"].
        """
        log.info("step3a_explore_start")

        review_tools = [
            make_list_project_files(self.repo_id),
            make_get_function_code(self.repo_id),
            make_get_class_code(self.repo_id),
            make_get_code_slice(self.repo_id),
            make_read_file(self.repo_id),
            make_search_in_file(self.repo_id),
            make_record_finding(self.repo_id, scratchpad),
            make_finalize_exploration(scratchpad),
        ]

        # Build lean orientation: code map header + existing issues list.
        # NO file contents here — the LLM pulls those itself via the tools.
        code_map = scratchpad.get("code_map", "")
        # Cap the orientation at ~10KB so the LLM's attention stays on the task,
        # not on memorizing the whole codebase.
        code_map_header = code_map[:10_000]

        existing = scratchpad.get("existing", {})
        existing_issues = existing.get("existing_issues", [])
        issue_lines = []
        for issue in existing_issues[:50]:
            labels = ", ".join(l.get("name", "") for l in issue.get("labels", []))
            issue_lines.append(
                f"- #{issue.get('number', '?')} {issue.get('title', '?')} [{labels}]"
            )

        user_content = (
            f"# Repository: {self.repo_full_name}\n\n"
            f"## Code Map Navigation (deterministic analysis)\n\n{code_map_header}\n\n"
            f"## Existing Issues ({len(existing_issues)} open)\n"
            + "\n".join(issue_lines) + "\n\n"
            "Explore the codebase using the tools. Record concrete findings via "
            "record_finding. When you're done, call finalize_exploration with a "
            "2-4 sentence project summary."
        )

        model = getattr(
            settings,
            "ai_model_onboarding_pass3a_explore",
            settings.ai_model_onboarding_pass3,
        )

        _, tool_call_log = await self._run_pass(
            pass_name="pass3a_explore",
            system_prompt=self._pass_prompts["pass3a_explore"],
            user_content=user_content,
            tools=review_tools,
            max_calls=20,  # tight budget — explorer should finalize at ~12-15 calls
            model=model,
        )

        # Log per-tool counts so we can see what the explorer actually did.
        # Without this, a run with 0 findings is indistinguishable from one
        # where the LLM read the whole repo but forgot to record anything.
        tool_counts: dict[str, int] = {}
        for tc in tool_call_log:
            name = tc.get("tool", "unknown")
            tool_counts[name] = tool_counts.get(name, 0) + 1
        log.info(
            "step3a_tool_breakdown",
            total=len(tool_call_log),
            by_tool=tool_counts,
            findings_recorded=len(scratchpad.get("findings", [])),
            finalized=bool(scratchpad.get("finalized")),
        )

        # FINAL CALL nudge: if the explorer exhausted its budget without
        # calling finalize_exploration, give it one more short pass with
        # an explicit reminder and a RESTRICTED toolset (only record_finding
        # and finalize_exploration — no more reads). This catches models
        # that burn all their turns on reads and forget to close out.
        if not scratchpad.get("finalized"):
            findings_so_far = len(scratchpad.get("findings", []))
            log.warning(
                "step3a_no_finalize",
                findings_so_far=findings_so_far,
                last_tool_calls=len(tool_call_log),
                note="retrying with FINAL CALL nudge",
            )
            final_call_system = (
                "You are wrapping up an exploration pass. You have no budget "
                "to read more code. Your only job is to call finalize_exploration "
                "with a project_summary based on the findings already recorded "
                "in the scratchpad."
            )
            nudge = (
                f"You exhausted your exploration budget without calling "
                f"finalize_exploration. You have {findings_so_far} findings "
                f"already recorded.\n\n"
                "DO NOT call any read/search/get tools — you have no budget "
                "to explore further.\n\n"
                "Your ONLY options:\n"
                "1. record_finding — if you have one more critical finding ready\n"
                "2. finalize_exploration — call this NOW with a 2-4 sentence "
                "project_summary that describes what this repository IS "
                "(tech stack, purpose, shape of the codebase).\n\n"
                "Call finalize_exploration immediately."
            )
            # Restricted toolset: no reads, so the LLM literally cannot waste
            # its remaining turns on exploration.
            final_call_tools = [
                make_record_finding(self.repo_id, scratchpad),
                make_finalize_exploration(scratchpad),
            ]
            try:
                await self._run_pass(
                    pass_name="pass3a_explore_final_call",
                    system_prompt=final_call_system,
                    user_content=nudge,
                    tools=final_call_tools,
                    max_calls=5,
                    model=model,
                )
            except Exception as e:
                log.warning("step3a_final_call_failed", error=str(e))

        findings = scratchpad.get("findings", [])
        log.info(
            "step3a_explore_complete",
            findings=len(findings),
            finalized=scratchpad.get("finalized", False),
            confidence=scratchpad.get("exploration_confidence"),
        )

    # ── Step 3b: Auditor (drop generic/unverifiable findings) ─────────────

    async def _step3b_audit(self, scratchpad: dict) -> list[dict]:
        """
        Second LLM review: drop generic, unverifiable, or duplicate findings.
        Returns the filtered list; logs counts.
        """
        findings = scratchpad.get("findings", [])
        if not findings:
            log.info("step3b_audit_skipped", reason="no findings to audit")
            return []

        log.info("step3b_audit_start", findings_in=len(findings))

        # Build the auditor input: findings + real file list for validation.
        real_files: list[str] = []
        try:
            # Real paths come from the code_map — cheap and accurate enough.
            for line in (scratchpad.get("code_map", "") or "").split("\n"):
                line = line.strip()
                if line.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".go",
                                  ".rs", ".java", ".rb", ".php", ".cs", ".kt",
                                  ".vue", ".svelte")):
                    real_files.append(line)
        except Exception:
            pass

        audit_input = json.dumps(
            {"file_list": real_files[:500], "findings": findings},
            default=str,
        )

        raw = await self._llm_call(
            self._pass_prompts["pass3b_audit"],
            audit_input,
            model=getattr(
                settings,
                "ai_model_onboarding_pass3b_audit",
                settings.ai_model_onboarding_pass3,
            ),
        )

        try:
            verdict = json.loads(_extract_json(raw))
        except json.JSONDecodeError:
            log.warning(
                "step3b_audit_parse_failed",
                reason="invalid JSON — keeping all findings unchanged",
                raw=raw[:500],
            )
            return findings

        kept_ids = {item.get("id") for item in verdict.get("kept", []) if item.get("id")}
        dropped = verdict.get("dropped", [])

        # Fail-open: if the auditor dropped EVERYTHING, keep the originals so
        # the grouper has something to work with. The auditor can't see the
        # actual code, so it routinely over-rejects legitimate findings — it's
        # better to let a minor finding through than to ship an empty plan.
        kept = [f for f in findings if f.get("id") in kept_ids]
        if not kept and findings:
            log.warning(
                "step3b_audit_empty_skipped",
                raw_findings=len(findings),
                reason="auditor dropped everything — keeping originals as fallback",
            )
            return findings

        log.info(
            "step3b_audit_complete",
            kept=len(kept),
            dropped=len(dropped),
        )
        return kept

    # ── Step 3c: Grouper (findings → milestones JSON) ─────────────────────

    async def _step3c_group(self, scratchpad: dict) -> dict[str, Any]:
        """
        Turn the audited findings list into the existing `suggested_plan`
        JSON shape so the dashboard's Issues tab keeps working unchanged.
        """
        findings = scratchpad.get("findings", [])
        existing = scratchpad.get("existing", {})
        existing_issues = existing.get("existing_issues", [])

        # Carry a short code map header so the grouper has navigation context
        code_map_header = (scratchpad.get("code_map", "") or "")[:3_000]
        project_summary = scratchpad.get("project_summary", "")

        grouper_input = json.dumps(
            {
                "project_summary": project_summary,
                "code_map_header": code_map_header,
                "findings": findings,
                "existing_issues": [
                    {
                        "number": i.get("number"),
                        "title": i.get("title"),
                        "labels": [l.get("name") for l in i.get("labels", [])],
                    }
                    for i in existing_issues
                ],
            },
            default=str,
        )

        log.info("step3c_group_start", findings=len(findings))

        raw = await self._llm_call(
            self._pass_prompts["pass3c_group"],
            grouper_input,
            model=getattr(
                settings,
                "ai_model_onboarding_pass3c_group",
                settings.ai_model_onboarding_pass3,
            ),
        )

        try:
            result = json.loads(_extract_json(raw))
        except json.JSONDecodeError:
            log.error("step3c_group_parse_failed", raw=raw[:500])
            raise RuntimeError("Step 3c (grouper) returned invalid JSON")

        # Schema guard: milestones must be a list, each with tasks.
        if not isinstance(result.get("milestones"), list):
            log.error("step3c_schema_drift", result_shape=list(result.keys()))
            raise RuntimeError("Step 3c returned a plan without a milestones list")

        if project_summary and not result.get("project_summary"):
            result["project_summary"] = project_summary

        log.info(
            "step3c_group_complete",
            milestones=len(result.get("milestones", [])),
            confidence=result.get("overall_confidence"),
        )
        return result

    # ── Legacy single-call fallback ───────────────────────────────────────

    async def _step3_milestones_legacy(self, scratchpad: dict) -> dict[str, Any]:
        """
        Original Step 3 behavior: one LLM call that reads the code map and
        produces milestones. Kept as a fallback for when the new agentic loop
        fails, or when `onboarding_use_agentic_review` is False.
        """
        log.info("step3_legacy_start")

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
        log.info("step3_legacy_context_size", chars=len(context))

        raw = await self._llm_call(
            self._pass_prompts["pass3_milestones"],
            context,
            model=settings.ai_model_onboarding_pass3,
        )

        try:
            result = json.loads(_extract_json(raw))
        except json.JSONDecodeError:
            log.error("step3_legacy_json_parse_failed", raw=raw[:500])
            raise RuntimeError("Step 3 legacy failed: LLM returned invalid JSON")

        log.info(
            "step3_legacy_complete",
            milestones=len(result.get("milestones", [])),
            confidence=result.get("overall_confidence"),
        )
        return result

    # -- Step 3.5: Validate Plan -------------------------------------------

    async def _gather_task_evidence(self, task_title: str, task_files: list[str]) -> dict:
        """
        Query the code_index to build an evidence-based completeness scorecard
        for a single task. Returns structured evidence the LLM can reason over.
        """
        evidence = {
            "files_found": [],
            "files_missing": [],
            "total_functions": 0,
            "total_classes": 0,
            "total_routes": 0,
            "total_lines": 0,
            "has_tests": False,
            "test_files": [],
            "has_error_handling": False,
            "has_validation": False,
            "completeness_signals": 0,
            "completeness_gaps": [],
        }

        for file_path in task_files[:5]:  # cap to avoid huge queries
            try:
                result = await _query_code_index(self.repo_id, file_path=file_path)
                if not result.success or not result.data:
                    evidence["files_missing"].append(file_path)
                    continue

                for record in result.data:
                    structure = record.get("structure", {})
                    functions = structure.get("functions", [])
                    classes = structure.get("classes", [])
                    routes = structure.get("routes", [])
                    lines = record.get("line_count", 0)

                    evidence["files_found"].append({
                        "path": record["file_path"],
                        "language": record["language"],
                        "lines": lines,
                        "functions": [f.get("name") for f in functions],
                        "classes": [c.get("name") for c in classes],
                        "routes": [f"{r.get('method', '')} {r.get('path', '')}" for r in routes],
                    })
                    evidence["total_functions"] += len(functions)
                    evidence["total_classes"] += len(classes)
                    evidence["total_routes"] += len(routes)
                    evidence["total_lines"] += lines

                    # Check for error handling patterns
                    func_names = [f.get("name", "").lower() for f in functions]
                    if any("error" in n or "exception" in n or "handler" in n for n in func_names):
                        evidence["has_error_handling"] = True

                    # Check for validation patterns
                    if any("valid" in n or "sanitiz" in n or "check" in n for n in func_names):
                        evidence["has_validation"] = True

            except Exception:
                evidence["files_missing"].append(file_path)

        # Check for test files covering these files
        for file_path in task_files[:5]:
            # Derive likely test file paths
            filename = file_path.split("/")[-1].replace(".py", "")
            test_patterns = [f"test_{filename}", f"test_{filename}.py", f"tests/test_{filename}"]
            for pattern in test_patterns:
                try:
                    test_result = await _query_code_index(self.repo_id, file_path=f"%{pattern}%")
                    if test_result.success and test_result.data:
                        evidence["has_tests"] = True
                        for tr in test_result.data:
                            evidence["test_files"].append(tr["file_path"])
                        break
                except Exception:
                    pass

        # Score completeness signals
        if evidence["files_found"]:
            evidence["completeness_signals"] += 1  # implementation exists
        if evidence["total_functions"] >= 3:
            evidence["completeness_signals"] += 1  # substantial code
        if evidence["total_routes"] >= 1:
            evidence["completeness_signals"] += 1  # API routes present
        if evidence["has_tests"]:
            evidence["completeness_signals"] += 1  # tests exist
        if evidence["has_error_handling"]:
            evidence["completeness_signals"] += 1  # error handling
        if evidence["has_validation"]:
            evidence["completeness_signals"] += 1  # validation logic

        # Identify gaps
        if not evidence["has_tests"]:
            evidence["completeness_gaps"].append("No test files found for this feature")
        if not evidence["has_error_handling"]:
            evidence["completeness_gaps"].append("No error handling functions detected")
        if not evidence["has_validation"]:
            evidence["completeness_gaps"].append("No input validation logic detected")
        if evidence["files_missing"]:
            evidence["completeness_gaps"].append(f"Referenced files not found: {evidence['files_missing']}")

        return evidence

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

                # Check status vs referenced files — gather EVIDENCE from code_index
                task_files = task.get("files", [])
                task_status = task.get("status", "not-started")
                if task_files and task_status == "not-started":
                    evidence = await self._gather_task_evidence(task_title, task_files)
                    flags.append({
                        "milestone_title": milestone.get("title", ""),
                        "task_title": task_title,
                        "flag_type": "status_check",
                        "evidence": evidence,
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
        Create sub-issues and Milestone Tracker issues deterministically.
        No LLM cost — executes the pass 3 plan directly in Python.
        """
        log.info("step4_start")

        milestones_data = scratchpad["milestones"]
        existing_titles = {
            i.get("title", "").lower()
            for i in scratchpad["existing"].get("existing_issues", [])
        }
        tool_call_log: list[dict] = []

        # Ensure the Milestone Tracker label exists. GitHub returns 422 if
        # it already exists — that's fine, we just wanted to guarantee it's
        # present, so swallow the error and keep going.
        try:
            label_result = await _create_label(
                self.installation_id, self.repo_full_name,
                name="Milestone Tracker", color="0052cc",
                description="Tracks milestone progress via linked sub-issues",
            )
            if not label_result.success:
                log.info(
                    "milestone_tracker_label_exists",
                    reason=label_result.error or "already exists",
                )
        except Exception as e:
            # Defensive: _create_label is supposed to return ToolResult, but
            # a raw httpx error got through before. Log and continue.
            log.info("milestone_tracker_label_create_skipped", error=str(e))

        for milestone in milestones_data.get("milestones", []):
            ms_title = milestone.get("title", "Untitled Milestone")
            tasks = milestone.get("tasks", [])
            task_numbers: list[tuple[str, int, str]] = []  # (title, number, status)

            # Create sub-issues for each task
            for task in tasks:
                title = task.get("title", "")
                if not title:
                    continue

                # Skip if existing issue already covers this
                if task.get("existing_issue"):
                    log.info("step4_skip_existing", title=title, existing=task["existing_issue"])
                    continue
                if title.lower() in existing_titles:
                    log.info("step4_skip_duplicate", title=title)
                    continue

                status = task.get("status", "not-started")
                labels = task.get("labels", ["enhancement"])
                files = task.get("files", [])

                # Build issue body
                if status == "done":
                    body = f"**Already implemented.**\n\n{task.get('description', '')}"
                    if files:
                        body += f"\n\n**Files:** {', '.join(f'`{f}`' for f in files)}"
                    if "done" not in labels:
                        labels.append("done")
                else:
                    body = task.get("description", "")
                    if files:
                        body += f"\n\n**Files to modify:** {', '.join(f'`{f}`' for f in files)}"

                result = await _create_issue(
                    self.installation_id, self.repo_full_name,
                    title=title, body=body, labels=labels,
                )
                if not result.success:
                    log.warning("step4_create_failed", title=title, error=result.error)
                    continue

                num = result.data.get("number", 0)
                task_numbers.append((title, num, status))
                tool_call_log.append({
                    "tool": "create_issue", "result": {"success": True},
                    "args": {"title": title},
                })
                log.info("step4_issue_created", title=title, number=num)

                # Persist in local DB
                await _save_issue_record(
                    self.repo_id, github_number=num, title=title,
                    state="open", labels=labels,
                )

                # Close done tasks immediately
                if status == "done" and num:
                    await _update_issue(
                        self.installation_id, self.repo_full_name,
                        num, state="closed",
                    )

            # Create the Milestone Tracker issue
            if task_numbers:
                checklist = "\n".join(
                    f"- [{'x' if s == 'done' else ' '}] {t} (#{n})"
                    for t, n, s in task_numbers
                )
                tracker_body = (
                    f"## {milestone.get('description', ms_title)}\n\n"
                    f"**Deadline:** TBD\n\n"
                    f"### Tasks\n{checklist}"
                )
                result = await _create_issue(
                    self.installation_id, self.repo_full_name,
                    title=ms_title, body=tracker_body,
                    labels=["Milestone Tracker"],
                )
                if result.success:
                    tracker_num = result.data.get("number", 0)
                    linked = [n for _, n, _ in task_numbers]
                    tool_call_log.append({
                        "tool": "create_issue", "result": {"success": True},
                        "args": {"title": ms_title, "labels": ["Milestone Tracker"]},
                    })
                    log.info("step4_tracker_created", title=ms_title, number=tracker_num)

                    # Persist tracker in local DB
                    await _save_issue_record(
                        self.repo_id, github_number=tracker_num, title=ms_title,
                        state="open", labels=["Milestone Tracker"],
                        is_milestone_tracker=True, linked_issue_numbers=linked,
                    )

                    # If ALL sub-issues are done, close the tracker too
                    all_done = all(s == "done" for _, _, s in task_numbers)
                    if all_done and tracker_num:
                        await _update_issue(
                            self.installation_id, self.repo_full_name,
                            tracker_num, state="closed",
                        )
                        log.info("step4_tracker_closed_all_done", title=ms_title, number=tracker_num)

        summary = f"Created {len(tool_call_log)} issues across {len(milestones_data.get('milestones', []))} milestones"
        log.info("step4_complete", tool_calls=len(tool_call_log))
        return summary, tool_call_log

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
                repo_id=self.repo_id,
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
                repo_id=self.repo_id,
            )
            tool_call_log.append({"tool": "flag_stale", "result": {"success": True}, "args": {"issue_number": issue_num}})
            log.info("progressive_issue_flagged", number=issue_num, reason=reason)

        summary = progressive.get("summary", "Progressive update complete")
        log.info("step4_progressive_complete", tool_calls=len(tool_call_log))
        return summary, tool_call_log

    # -- Step 5: Reply to recent comments -----------------------------------

    async def _step5_reply_to_comments(self, scratchpad: dict) -> list[dict]:
        """
        Scan existing issues for recent human comments that GITA hasn't replied to.
        Picks the 10 most recent human comments across all issues and replies if useful.
        """
        from src.core.github_auth import GitHubClient

        log.info("step5_comments_start")
        tool_call_log = []

        existing_issues = scratchpad.get("existing", {}).get("existing_issues", [])
        if not existing_issues:
            log.info("step5_no_issues")
            return tool_call_log

        # Collect recent human comments across all issues
        client = GitHubClient(self.installation_id)
        all_comments = []

        for issue in existing_issues[:20]:  # cap to avoid too many API calls
            issue_num = issue.get("number", 0)
            if not issue_num:
                continue
            try:
                comments = await client.get(
                    f"/repos/{self.repo_full_name}/issues/{issue_num}/comments",
                    params={"per_page": 5, "sort": "created", "direction": "desc"},
                )
                for c in (comments or []):
                    user = c.get("user", {})
                    login = user.get("login", "")
                    is_bot = user.get("type") == "Bot" or login.endswith("[bot]")
                    if not is_bot and c.get("body", "").strip():
                        all_comments.append({
                            "issue_number": issue_num,
                            "issue_title": issue.get("title", ""),
                            "comment_id": c.get("id"),
                            "author": login,
                            "body": c.get("body", "")[:1000],
                            "created_at": c.get("created_at", ""),
                        })
            except Exception:
                continue

        if not all_comments:
            log.info("step5_no_human_comments")
            return tool_call_log

        # Sort by recency and take top 10
        all_comments.sort(key=lambda c: c.get("created_at", ""), reverse=True)
        top_comments = all_comments[:10]

        log.info("step5_found_comments", count=len(top_comments))

        # Check which ones already have a GITA reply after them
        comments_to_reply = []
        for comment in top_comments:
            issue_num = comment["issue_number"]
            try:
                issue_comments = await client.get(
                    f"/repos/{self.repo_full_name}/issues/{issue_num}/comments",
                    params={"per_page": 20},
                )
                # Check if GITA already replied after this comment
                found_comment = False
                gita_replied = False
                for ic in (issue_comments or []):
                    if ic.get("id") == comment["comment_id"]:
                        found_comment = True
                        continue
                    if found_comment:
                        ic_user = ic.get("user", {})
                        if ic_user.get("type") == "Bot" or ic_user.get("login", "").endswith("[bot]"):
                            gita_replied = True
                            break

                if not gita_replied:
                    comments_to_reply.append(comment)
            except Exception:
                comments_to_reply.append(comment)  # if we can't check, try anyway

        if not comments_to_reply:
            log.info("step5_all_replied")
            return tool_call_log

        log.info("step5_replying", count=len(comments_to_reply))

        # Use the LLM to generate replies
        for comment in comments_to_reply[:5]:  # cap at 5 replies per onboarding
            try:
                context = json.dumps({
                    "task": "Reply to this comment on a project issue",
                    "repo": self.repo_full_name,
                    "issue_number": comment["issue_number"],
                    "issue_title": comment["issue_title"],
                    "comment_author": comment["author"],
                    "comment_body": comment["body"],
                    "instructions": (
                        f"@{comment['author']} commented on issue #{comment['issue_number']} "
                        f"(\"{comment['issue_title']}\"): \"{comment['body'][:300]}\"\n\n"
                        "Write a brief, helpful reply. Be conversational and constructive. "
                        "If you can offer specific advice related to the issue, do so. "
                        "If the comment is a question, answer it. "
                        "If it's a suggestion, acknowledge and respond. "
                        "Keep it under 200 words. End with the GITA signature."
                    ),
                })

                raw, calls = await self._run_pass(
                    "step5_comment_reply",
                    "You are GITA, a helpful AI assistant for this GitHub project. "
                    "You are replying to a comment on an issue you created. Be helpful, "
                    "concise, and reference the issue context. Always end with: "
                    "---\\n*Generated by GitHub Assistant*",
                    context,
                    tools=[],
                    max_calls=0,
                    model=settings.ai_model_issue_analyst,
                )

                if raw and raw.strip():
                    reply_body = raw.strip()
                    if "Generated by GitHub Assistant" not in reply_body:
                        reply_body += "\n\n---\n*Generated by GitHub Assistant*"

                    result = await _post_comment(
                        self.installation_id, self.repo_full_name,
                        comment["issue_number"], reply_body,
                        repo_id=self.repo_id,
                    )
                    if result.success:
                        tool_call_log.append({
                            "tool": "reply_to_comment",
                            "result": {"success": True},
                            "args": {"issue_number": comment["issue_number"], "reply_to": comment["author"]},
                        })
                        log.info("step5_replied", issue=comment["issue_number"], author=comment["author"])
            except Exception as e:
                log.warning("step5_reply_failed", issue=comment["issue_number"], error=str(e))

        return tool_call_log

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

        # Step 5: Respond to recent human comments on existing issues
        try:
            comment_replies = await self._step5_reply_to_comments(scratchpad)
            all_tool_calls.extend(comment_replies)
        except Exception as e:
            log.error("step5_comments_failed", error=str(e))

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
