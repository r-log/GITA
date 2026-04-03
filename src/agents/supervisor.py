"""
Supervisor Agent — the event router.

Receives every webhook event, classifies it, and dispatches to the right
specialist agent(s). Can run multiple agents in parallel.
Never does analysis itself — delegates everything.
"""

from __future__ import annotations

import asyncio
import json
import time
import structlog
from dataclasses import asdict
from datetime import datetime, timedelta

import re

from src.agents.base import BaseAgent, AgentContext, AgentResult
from src.agents.registry import registry
from src.tools.base import Tool, ToolResult
from src.core.config import settings
from src.core.database import async_session
from src.models.agent_run import AgentRun

log = structlog.get_logger()


def _extract_json(text: str) -> str:
    """Extract JSON from LLM response that may include prose or code fences."""
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return text
    fence_match = re.search(r"```(?:json)?\s*\n(\{[\s\S]*?\})\s*```", text)
    if fence_match:
        return fence_match.group(1).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return text
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        return text[first_brace:last_brace + 1]
    return text


class SupervisorAgent(BaseAgent):
    """
    The Supervisor classifies incoming webhook events and dispatches
    specialist agents. It doesn't do analysis — it routes.
    """

    def __init__(self):
        # Supervisor has no tools — it only reasons about routing
        super().__init__(
            name="supervisor",
            description="Event router that dispatches specialist agents based on webhook events",
            tools=[],
            model=settings.ai_model_supervisor,
            system_prompt_file="supervisor.md",
        )

    async def handle(self, context: AgentContext) -> AgentResult:
        started_at = time.time()
        dispatch_plan = await self._classify_and_plan(context)

        agent_names = dispatch_plan.get("agents_to_dispatch", [])
        run_parallel = dispatch_plan.get("parallel", False)

        log.info(
            "supervisor_dispatch",
            webhook_event=context.event_type,
            agents=agent_names,
            parallel=run_parallel,
            reasoning=dispatch_plan.get("reasoning"),
        )

        if not agent_names:
            return AgentResult(
                agent_name=self.name,
                status="success",
                data={"dispatch_plan": dispatch_plan, "message": "No agents dispatched"},
            )

        # Check cooldown — skip agents that recently ran on the same target
        target_number = self._extract_target_number(context.event_payload)
        if target_number and context.repo_id:
            cooled = await self._check_cooldown(context.repo_id, target_number, agent_names)
            if cooled:
                log.info("cooldown_filtered", removed=cooled)
                agent_names = [a for a in agent_names if a not in cooled]
                if not agent_names:
                    return AgentResult(
                        agent_name=self.name,
                        status="success",
                        data={"dispatch_plan": dispatch_plan, "message": "All agents on cooldown"},
                    )

        # Dispatch agents
        results = await self._dispatch_agents(agent_names, context, run_parallel)

        # Merge results
        merged = self._merge_results(results, dispatch_plan)
        merged.data["duration_ms"] = int((time.time() - started_at) * 1000)

        return merged

    async def _classify_and_plan(self, context: AgentContext) -> dict:
        """Use the LLM to classify the event and decide which agents to dispatch."""
        available_agents = registry.list_agents()

        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": json.dumps({
                    "event_type": context.event_type,
                    "action": context.event_payload.get("action"),
                    "repo": context.repo_full_name,
                    "available_agents": available_agents,
                    "payload_summary": self._summarize_payload(context.event_payload),
                }, indent=2),
            },
        ]

        response = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        try:
            raw = response.choices[0].message.content or ""
            raw = _extract_json(raw)
            return json.loads(raw)
        except (json.JSONDecodeError, IndexError):
            log.error("supervisor_parse_error", response=response.choices[0].message.content)
            return {"agents_to_dispatch": [], "reasoning": "Failed to parse LLM response"}

    async def _dispatch_agents(
        self,
        agent_names: list[str],
        context: AgentContext,
        parallel: bool,
    ) -> list[AgentResult]:
        """Dispatch specialist agents, optionally in parallel."""
        results = []

        async def _run_one(name: str) -> AgentResult:
            agent = registry.get(name, context)
            if not agent:
                log.warning("agent_not_found", name=name)
                return AgentResult(agent_name=name, status="failed", data={"error": f"Agent '{name}' not registered"})

            run_id = await self._log_agent_start(name, context)
            started = time.time()

            try:
                result = await asyncio.wait_for(
                    agent.handle(context),
                    timeout=settings.agent_timeout_seconds,
                )
                result.data["agent_run_id"] = run_id
                await self._log_agent_complete(run_id, result, started)
                return result

            except asyncio.TimeoutError:
                result = AgentResult(agent_name=name, status="failed", data={"error": "Agent timed out"})
                await self._log_agent_complete(run_id, result, started, error="Timeout")
                return result
            except Exception as e:
                log.exception("agent_error", agent=name)
                result = AgentResult(agent_name=name, status="failed", data={"error": str(e)})
                await self._log_agent_complete(run_id, result, started, error=str(e))
                return result

        if parallel and len(agent_names) > 1:
            results = await asyncio.gather(*[_run_one(n) for n in agent_names])
        else:
            for name in agent_names:
                results.append(await _run_one(name))

        return list(results)

    def _merge_results(self, results: list[AgentResult], dispatch_plan: dict) -> AgentResult:
        """Merge results from multiple agents into a single Supervisor result."""
        all_actions = []
        all_recommendations = []
        all_data = {"dispatch_plan": dispatch_plan, "agent_results": {}}
        should_notify = False
        comment_parts = []
        worst_status = "success"

        status_priority = {"success": 0, "partial": 1, "needs_review": 2, "failed": 3}

        for r in results:
            all_actions.extend(r.actions_taken)
            all_recommendations.extend(r.recommendations)
            all_data["agent_results"][r.agent_name] = r.to_dict()

            if r.should_notify:
                should_notify = True
                if r.comment_body:
                    comment_parts.append(r.comment_body)

            if status_priority.get(r.status, 0) > status_priority.get(worst_status, 0):
                worst_status = r.status

        return AgentResult(
            agent_name=self.name,
            status=worst_status,
            actions_taken=all_actions,
            recommendations=all_recommendations,
            data=all_data,
            should_notify=should_notify,
            comment_body="\n\n---\n\n".join(comment_parts) if comment_parts else None,
        )

    def _summarize_payload(self, payload: dict) -> dict:
        """Extract the important parts of a webhook payload for the LLM."""
        summary = {}
        # Common fields
        for key in ("action", "sender", "repository"):
            if key in payload:
                if key == "sender":
                    summary[key] = payload[key].get("login")
                elif key == "repository":
                    summary[key] = payload[key].get("full_name")
                else:
                    summary[key] = payload[key]

        # Event-specific fields
        for key in ("issue", "pull_request", "milestone", "comment"):
            if key in payload:
                obj = payload[key]
                summary[key] = {
                    "number": obj.get("number"),
                    "title": obj.get("title"),
                    "state": obj.get("state"),
                    "user": obj.get("user", {}).get("login"),
                }
                if key == "pull_request":
                    summary[key]["base"] = obj.get("base", {}).get("ref")
                    summary[key]["head"] = obj.get("head", {}).get("ref")

        if "ref" in payload:
            summary["ref"] = payload["ref"]
        if "commits" in payload:
            summary["commits_count"] = len(payload["commits"])

        return summary

    async def _log_agent_start(self, agent_name: str, context: AgentContext) -> int | None:
        """Log agent run start to DB. Returns the run ID, or None if no repo_id."""
        if not context.repo_id:
            return None
        try:
            async with async_session() as session:
                run = AgentRun(
                    repo_id=context.repo_id,
                    agent_name=agent_name,
                    event_type=context.event_type,
                    context={
                        "event_type": context.event_type,
                        "repo_full_name": context.repo_full_name,
                        "installation_id": context.installation_id,
                    },
                    status="running",
                )
                session.add(run)
                await session.commit()
                await session.refresh(run)
                return run.id
        except Exception as e:
            log.warning("log_agent_start_failed", error=str(e))
            return None

    async def _log_agent_complete(
        self, run_id: int | None, result: AgentResult, started: float, error: str | None = None
    ) -> None:
        """Update agent run record with result."""
        if not run_id:
            return
        async with async_session() as session:
            from sqlalchemy import update
            stmt = (
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    status=result.status,
                    result=result.to_dict(),
                    tools_called=result.data.get("tool_call_log", []),
                    confidence=result.confidence,
                    duration_ms=int((time.time() - started) * 1000),
                    error_message=error,
                    completed_at=datetime.utcnow(),
                )
            )
            await session.execute(stmt)
            await session.commit()

    def _extract_target_number(self, payload: dict) -> int | None:
        """Extract the issue/PR/milestone number from a webhook payload."""
        for key in ("issue", "pull_request", "milestone"):
            if key in payload and "number" in payload[key]:
                return payload[key]["number"]
        return None

    async def _check_cooldown(self, repo_id: int, target_number: int, agent_names: list[str]) -> list[str]:
        """Check which agents are on cooldown for this target. Returns names to skip."""
        from sqlalchemy import select
        cooled = []
        cutoff = datetime.utcnow() - timedelta(minutes=settings.comment_cooldown_minutes)

        async with async_session() as session:
            for name in agent_names:
                stmt = (
                    select(AgentRun)
                    .where(
                        AgentRun.repo_id == repo_id,
                        AgentRun.agent_name == name,
                        AgentRun.status == "success",
                        AgentRun.completed_at > cutoff,
                    )
                    .limit(1)
                )
                result = await session.execute(stmt)
                if result.scalar_one_or_none():
                    cooled.append(name)

        return cooled
