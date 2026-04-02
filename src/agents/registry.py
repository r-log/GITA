"""
Agent registry — lookup agents by name for dispatch.

Supports two types of agents:
- Singleton agents: instantiated once, stateless (e.g. future simple agents)
- Factory agents: created per-request with context (installation_id, repo, etc.)
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from src.agents.base import BaseAgent, AgentContext


# Type for a factory that creates an agent given context
AgentFactory = Callable[[AgentContext], BaseAgent]


class AgentRegistry:
    """
    Central registry of all specialist agents.

    The Supervisor uses this to look up agents by name when dispatching.
    New agents register themselves here — zero changes to existing code.
    """

    def __init__(self):
        self._singletons: Dict[str, BaseAgent] = {}
        self._factories: Dict[str, AgentFactory] = {}
        self._descriptions: Dict[str, str] = {}

    def register(self, agent: BaseAgent) -> None:
        """Register a singleton agent by its name."""
        self._singletons[agent.name] = agent
        self._descriptions[agent.name] = agent.description

    def register_factory(self, name: str, description: str, factory: AgentFactory) -> None:
        """Register a factory that creates agents per-request with context."""
        self._factories[name] = factory
        self._descriptions[name] = description

    def get(self, name: str, context: Optional[AgentContext] = None) -> Optional[BaseAgent]:
        """Look up an agent by name. Factory agents require context."""
        if name in self._singletons:
            return self._singletons[name]
        if name in self._factories and context:
            return self._factories[name](context)
        return None

    def list_agents(self) -> list[dict]:
        """List all registered agents (for Supervisor context)."""
        return [
            {"name": name, "description": desc}
            for name, desc in self._descriptions.items()
        ]

    @property
    def names(self) -> list[str]:
        return list(self._descriptions.keys())


# Singleton — import this everywhere
registry = AgentRegistry()
