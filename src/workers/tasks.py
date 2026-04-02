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

    # Resolve repo_id — upsert the repository record
    repo_github_id = payload.get("repository", {}).get("id", 0)
    repo_id = 0
    if repo_github_id:
        repo_id = await upsert_repository(repo_github_id, repo_full_name, installation_id)

    context = AgentContext(
        event_type=full_event,
        event_payload=payload,
        repo_full_name=repo_full_name,
        installation_id=installation_id,
        repo_id=repo_id,
    )

    supervisor = _get_supervisor()

    log.info("dispatch_event", event=full_event, repo=repo_full_name, repo_id=repo_id)

    result = await supervisor.handle(context)

    log.info(
        "dispatch_complete",
        event=full_event,
        status=result.status,
        agents_dispatched=list(result.data.get("agent_results", {}).keys()),
    )
