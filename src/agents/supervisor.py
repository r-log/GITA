"""
Supervisor Agent — the event router.

Receives every webhook event, classifies it via a static routing table,
and dispatches to the right specialist agent(s). Can run multiple agents
in parallel. Never does analysis itself — delegates everything.
"""

from __future__ import annotations

import asyncio
import time
import structlog
from datetime import datetime, timedelta

from src.agents.base import AgentContext, AgentResult
from src.agents.registry import registry
from src.core.config import settings
from src.core.database import async_session
from src.models.agent_run import AgentRun
from src.tools.github.pull_requests import _get_pr_diff, _get_pr_files
from src.tools.db.graph_queries import _get_blast_radius

log = structlog.get_logger()

# Static routing: event_type → (agent_names, parallel)
ROUTING_TABLE: dict[str, tuple[list[str], bool]] = {
    "installation.created":            (["onboarding"], False),
    "installation_repositories.added": (["onboarding"], False),
    "pull_request.opened":             (["pr_reviewer", "risk_detective"], True),
    "pull_request.synchronize":        (["pr_reviewer", "risk_detective"], True),
    "issues.opened":                   (["issue_analyst"], False),
    "issues.edited":                   (["issue_analyst", "progress_tracker"], True),
    "issues.assigned":                 (["issue_analyst"], False),
    "issues.closed":                   (["issue_analyst", "progress_tracker"], True),
    "issues.milestoned":               (["issue_analyst", "progress_tracker"], True),
    "push":                            (["progress_tracker", "risk_detective"], True),
    "issue_comment.created":           (["issue_analyst"], False),
}


class SupervisorAgent:
    """
    The Supervisor classifies incoming webhook events and dispatches
    specialist agents. It doesn't do analysis — it routes.
    """

    name = "supervisor"

    async def handle(self, context: AgentContext) -> AgentResult:
        started_at = time.time()
        dispatch_plan = self._classify_and_plan(context)

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

        # Pre-gather shared data for PR events (avoids duplicate API calls
        # when PR Reviewer and Risk Detective both need the same diff/files/blast radius)
        if context.event_type.startswith("pull_request.") and len(agent_names) > 1:
            await self._pre_gather_pr_data(context)

        # Dispatch agents
        results = await self._dispatch_agents(agent_names, context, run_parallel)

        # Merge results
        merged = self._merge_results(results, dispatch_plan)
        merged.data["duration_ms"] = int((time.time() - started_at) * 1000)

        return merged

    async def _pre_gather_pr_data(self, context: AgentContext) -> None:
        """
        Pre-gather common PR data that multiple agents need.
        Stores in context.additional_data["pr_gathered"] so both
        PR Reviewer and Risk Detective can read from it instead of
        making duplicate API calls.
        """
        pr_data = context.event_payload.get("pull_request", {})
        pr_number = pr_data.get("number", 0)
        if not pr_number:
            return

        log.info("supervisor_pre_gather", pr=pr_number)
        gathered = {}

        try:
            # Fetch changed files (also persists to pr_file_changes via side-effect)
            files_result = await _get_pr_files(
                context.installation_id, context.repo_full_name,
                pr_number, context.repo_id,
            )
            gathered["files"] = files_result.data if files_result.success else []

            # Fetch diff
            diff_result = await _get_pr_diff(
                context.installation_id, context.repo_full_name, pr_number,
            )
            gathered["diff"] = diff_result.data.get("diff", "") if diff_result.success else ""

            # Blast radius
            file_paths = [f["filename"] for f in gathered["files"]] if gathered["files"] else []
            if file_paths and context.repo_id:
                blast_result = await _get_blast_radius(context.repo_id, file_paths, depth=2)
                gathered["blast_radius"] = blast_result.data if blast_result.success else {}
            else:
                gathered["blast_radius"] = {}

            context.additional_data["pr_gathered"] = gathered
            log.info(
                "supervisor_pre_gather_complete",
                pr=pr_number,
                files=len(gathered["files"]),
                diff_size=len(gathered["diff"]),
            )
        except Exception as e:
            log.warning("supervisor_pre_gather_failed", pr=pr_number, error=str(e))

    def _classify_and_plan(self, context: AgentContext) -> dict:
        """Look up the routing table and return a dispatch plan."""
        agents, parallel = ROUTING_TABLE.get(context.event_type, ([], False))

        return {
            "event_summary": f"Routing {context.event_type} for {context.repo_full_name}",
            "agents_to_dispatch": list(agents),
            "reasoning": "static routing table",
            "parallel": parallel,
            "priority": "normal",
        }

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
                result.data["usage"] = dict(agent._usage)
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
