"""Tests for src.agents.registry — agent registration and lookup."""

import pytest

from src.agents.base import AgentContext, AgentResult, BaseAgent
from src.agents.registry import AgentRegistry
from src.tools.base import Tool


# --- Minimal concrete agent for testing ---

class DummyAgent(BaseAgent):
    def __init__(self, name="dummy", description="A dummy agent"):
        # Bypass BaseAgent.__init__ to avoid OpenAI client creation
        self.name = name
        self.description = description
        self.tools = []
        self.model = "test-model"
        self._usage = {}
        self._tool_map = {}
        self.system_prompt = "test"

    async def handle(self, context: AgentContext) -> AgentResult:
        return AgentResult(agent_name=self.name, status="success")


@pytest.fixture
def fresh_registry():
    """Create a fresh registry for each test (avoid shared state)."""
    return AgentRegistry()


class TestSingletonRegistration:
    def test_register_and_get(self, fresh_registry):
        agent = DummyAgent("issue_analyst", "Analyzes issues")
        fresh_registry.register(agent)

        result = fresh_registry.get("issue_analyst")
        assert result is agent

    def test_get_unknown_returns_none(self, fresh_registry):
        assert fresh_registry.get("nonexistent") is None

    def test_overwrite_registration(self, fresh_registry):
        agent1 = DummyAgent("agent_a", "First")
        agent2 = DummyAgent("agent_a", "Second")
        fresh_registry.register(agent1)
        fresh_registry.register(agent2)

        assert fresh_registry.get("agent_a") is agent2


class TestFactoryRegistration:
    def test_register_factory_and_get_with_context(self, fresh_registry):
        def factory(ctx: AgentContext) -> BaseAgent:
            return DummyAgent("pr_reviewer", f"For {ctx.repo_full_name}")

        fresh_registry.register_factory("pr_reviewer", "Reviews PRs", factory)
        ctx = AgentContext(
            event_type="pull_request.opened",
            event_payload={},
            repo_full_name="owner/repo",
            installation_id=1,
        )

        agent = fresh_registry.get("pr_reviewer", ctx)
        assert agent is not None
        assert agent.name == "pr_reviewer"

    def test_factory_without_context_returns_none(self, fresh_registry):
        fresh_registry.register_factory("pr_reviewer", "Reviews PRs", lambda ctx: DummyAgent())
        assert fresh_registry.get("pr_reviewer") is None

    def test_factory_receives_context(self, fresh_registry):
        received_contexts = []

        def factory(ctx: AgentContext) -> BaseAgent:
            received_contexts.append(ctx)
            return DummyAgent()

        fresh_registry.register_factory("tracker", "Tracks", factory)
        ctx = AgentContext(
            event_type="push",
            event_payload={"ref": "refs/heads/main"},
            repo_full_name="owner/repo",
            installation_id=42,
        )
        fresh_registry.get("tracker", ctx)

        assert len(received_contexts) == 1
        assert received_contexts[0].installation_id == 42


class TestListAgents:
    def test_list_agents(self, fresh_registry):
        fresh_registry.register(DummyAgent("agent_a", "Does A"))
        fresh_registry.register_factory("agent_b", "Does B", lambda ctx: DummyAgent())

        agents = fresh_registry.list_agents()
        names = [a["name"] for a in agents]
        assert "agent_a" in names
        assert "agent_b" in names

    def test_names_property(self, fresh_registry):
        fresh_registry.register(DummyAgent("x", "X"))
        fresh_registry.register_factory("y", "Y", lambda ctx: DummyAgent())

        assert set(fresh_registry.names) == {"x", "y"}

    def test_empty_registry(self, fresh_registry):
        assert fresh_registry.list_agents() == []
        assert fresh_registry.names == []
