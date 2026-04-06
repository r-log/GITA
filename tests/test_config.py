"""Tests for src.core.config — Settings loading and properties."""

from src.core.config import settings


class TestSettings:
    def test_github_app_id_loaded(self):
        assert settings.github_app_id == 12345

    def test_webhook_secret_loaded(self):
        assert settings.github_webhook_secret == "test-webhook-secret"

    def test_default_model(self):
        assert "claude" in settings.ai_default_model or settings.ai_default_model

    def test_is_dev_in_testing(self):
        # We set ENVIRONMENT=testing in conftest
        assert settings.environment == "testing"
        assert settings.is_dev is False

    def test_model_pricing_returns_dict(self):
        pricing = settings.model_pricing
        assert isinstance(pricing, dict)
        assert len(pricing) > 0
        # Each entry should be (input_cost, output_cost) tuple
        for model, costs in pricing.items():
            assert len(costs) == 2

    def test_agent_defaults(self):
        assert settings.agent_max_tool_calls > 0
        assert settings.agent_timeout_seconds > 0
