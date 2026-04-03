"""
Background task functions for agent dispatch.

In production, these run inside the ARQ worker process.
During early development (no worker running), they execute inline.
"""

import structlog

from src.agents.base import AgentContext
from src.agents.supervisor import SupervisorAgent
from src.core.repo_manager import upsert_repository

log = structlog.get_logger()

# Singleton supervisor (stateless, safe to reuse)
_supervisor: SupervisorAgent | None = None


def _get_supervisor() -> SupervisorAgent:
    global _supervisor
    if _supervisor is None:
        _supervisor = SupervisorAgent()
    return _supervisor


async def dispatch_event(
    event_type: str,
    action: str,
    repo_full_name: str,
    installation_id: int,
    payload: dict,
) -> None:
    """
    Dispatch a webhook event to the Supervisor Agent.

    Resolves repo_id from DB (upserts if new), then passes everything
    to the Supervisor for classification and agent dispatch.
    """
    full_event = f"{event_type}.{action}" if action else event_type

    # Resolve repo_id — handle both repo-level and installation-level events
    repo_github_id = payload.get("repository", {}).get("id", 0)
    repo_id = 0

    if repo_github_id:
        # Normal repo-level event (issues, PRs, push, etc.)
        repo_id = await upsert_repository(repo_github_id, repo_full_name, installation_id)
    elif event_type == "installation" and payload.get("repositories"):
        # Installation event — has a list of repos, dispatch for each
        for repo_info in payload["repositories"]:
            r_name = repo_info.get("full_name", "")
            r_id = repo_info.get("id", 0)
            if r_id and r_name:
                db_repo_id = await upsert_repository(r_id, r_name, installation_id)
                ctx = AgentContext(
                    event_type=full_event,
                    event_payload=payload,
                    repo_full_name=r_name,
                    installation_id=installation_id,
                    repo_id=db_repo_id,
                )
                supervisor = _get_supervisor()
                log.info("dispatch_event", webhook_event=full_event, repo=r_name, repo_id=db_repo_id)
                result = await supervisor.handle(ctx)
                log.info("dispatch_complete", webhook_event=full_event, status=result.status,
                         agents_dispatched=list(result.data.get("agent_results", {}).keys()))
        return

    elif event_type == "installation_repositories":
        # Repos added/removed from an existing installation
        repo_list_key = "repositories_added" if action == "added" else "repositories_removed"
        repos = payload.get(repo_list_key, [])
        for repo_info in repos:
            r_name = repo_info.get("full_name", "")
            r_id = repo_info.get("id", 0)
            if r_id and r_name:
                db_repo_id = await upsert_repository(r_id, r_name, installation_id)
                ctx = AgentContext(
                    event_type=full_event,
                    event_payload=payload,
                    repo_full_name=r_name,
                    installation_id=installation_id,
                    repo_id=db_repo_id,
                )
                supervisor = _get_supervisor()
                log.info("dispatch_event", webhook_event=full_event, repo=r_name, repo_id=db_repo_id)
                result = await supervisor.handle(ctx)
                log.info("dispatch_complete", webhook_event=full_event, status=result.status,
                         agents_dispatched=list(result.data.get("agent_results", {}).keys()))
        return

    context = AgentContext(
        event_type=full_event,
        event_payload=payload,
        repo_full_name=repo_full_name,
        installation_id=installation_id,
        repo_id=repo_id,
    )

    supervisor = _get_supervisor()

    log.info("dispatch_event", webhook_event=full_event, repo=repo_full_name, repo_id=repo_id)

    result = await supervisor.handle(context)

    log.info(
        "dispatch_complete",
        webhook_event=full_event,
        status=result.status,
        agents_dispatched=list(result.data.get("agent_results", {}).keys()),
    )
