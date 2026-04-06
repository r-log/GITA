"""Tests for src.agents.setup — agent registration."""

from unittest.mock import patch

from src.agents.setup import register_all_agents


class TestRegisterAllAgents:
    @patch("src.agents.setup.registry")
    def test_registers_all_five_agents(self, mock_registry):
        register_all_agents()
        assert mock_registry.register_factory.call_count == 5

    @patch("src.agents.setup.registry")
    def test_agent_names_registered(self, mock_registry):
        register_all_agents()
        registered_names = [call.kwargs.get("name", call.args[0] if call.args else None)
                            for call in mock_registry.register_factory.call_args_list]
        assert "onboarding" in registered_names
        assert "issue_analyst" in registered_names
        assert "progress_tracker" in registered_names
        assert "pr_reviewer" in registered_names
        assert "risk_detective" in registered_names

    @patch("src.agents.setup.registry")
    def test_factories_have_descriptions(self, mock_registry):
        register_all_agents()
        for call in mock_registry.register_factory.call_args_list:
            description = call.kwargs.get("description", call.args[1] if len(call.args) > 1 else "")
            assert len(description) > 0
