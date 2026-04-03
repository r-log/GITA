"""
Application configuration loaded from environment variables.
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional
from pathlib import Path


class Settings(BaseSettings):
    """All configuration in one place, loaded from .env file."""

    # --- GitHub App ---
    github_app_id: int
    github_app_private_key_path: Path = Field(default=Path("./github-app-private-key.pem"))
    github_webhook_secret: str

    # --- Database ---
    database_url: str = "postgresql+asyncpg://github_assistant:password@db:5432/github_assistant"

    # --- Redis ---
    redis_url: str = "redis://redis:6379"

    # --- AI Provider (OpenRouter) ---
    openrouter_api_key: str
    ai_default_model: str = "anthropic/claude-sonnet-4"

    # --- Per-Agent Model Overrides (OpenRouter model IDs) ---
    # Each agent/pass can use a different model. Override via env vars.
    ai_model_supervisor: str = "anthropic/claude-haiku-4.5"
    ai_model_onboarding_pass1: str = "anthropic/claude-haiku-4.5"
    ai_model_onboarding_pass2: str = "anthropic/claude-sonnet-4"
    ai_model_onboarding_pass3: str = "anthropic/claude-sonnet-4"
    ai_model_onboarding_pass3_5: str = "anthropic/claude-sonnet-4"
    ai_model_onboarding_pass4: str = "anthropic/claude-haiku-4.5"
    ai_model_issue_analyst: str = "anthropic/claude-sonnet-4"
    ai_model_progress_tracker: str = "anthropic/claude-sonnet-4"
    ai_model_pr_reviewer: str = "anthropic/claude-sonnet-4"
    ai_model_risk_detective: str = "anthropic/claude-opus-4.5"

    # --- Per-Tool Model Overrides ---
    ai_model_project_planner: str = "anthropic/claude-sonnet-4"
    ai_model_plan_reconciler: str = "anthropic/claude-haiku-4.5"
    ai_model_smart_evaluator: str = "anthropic/claude-sonnet-4"
    ai_model_milestone_alignment: str = "anthropic/claude-haiku-4.5"
    ai_model_diff_analyzer: str = "anthropic/claude-sonnet-4"
    ai_model_test_coverage: str = "anthropic/claude-sonnet-4"
    ai_model_secret_scanner: str = "anthropic/claude-opus-4.5"
    ai_model_security_scanner: str = "anthropic/claude-opus-4.5"
    ai_model_breaking_changes: str = "anthropic/claude-sonnet-4"
    ai_model_dependency_checker: str = "anthropic/claude-sonnet-4"
    ai_model_predictor: str = "anthropic/claude-haiku-4.5"
    ai_model_context_updater: str = "anthropic/claude-haiku-4.5"

    # --- Reconciliation ---
    reconciliation_interval_hours: int = 6

    # --- App ---
    log_level: str = "INFO"
    environment: str = "development"
    webhook_proxy_url: Optional[str] = None

    # --- Agent Defaults ---
    agent_max_tool_calls: int = 150      # safety limit per agent run
    agent_timeout_seconds: int = 900     # max time for a single agent run
    comment_cooldown_minutes: int = 0    # disabled for testing

    @property
    def github_app_private_key(self) -> str:
        """Read the private key from file."""
        return self.github_app_private_key_path.read_text()

    @property
    def is_dev(self) -> bool:
        return self.environment == "development"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


# Singleton — import this everywhere
settings = Settings()
