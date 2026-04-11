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

    # --- AI Provider ---
    # Supports any OpenAI-compatible API: OpenRouter, Anthropic, Ollama, Together, Kimi, etc.
    # Legacy: openrouter_api_key still works as fallback for llm_api_key
    openrouter_api_key: str = ""
    llm_api_key: str = ""
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout: float = 120.0
    ai_default_model: str = "anthropic/claude-sonnet-4"

    @property
    def effective_api_key(self) -> str:
        """LLM_API_KEY takes priority, falls back to OPENROUTER_API_KEY for backwards compat."""
        return self.llm_api_key or self.openrouter_api_key

    # --- Per-Agent Model Overrides (OpenRouter model IDs) ---
    # Each agent/pass can use a different model. Override via env vars.
    # Pass 1 (index) and Pass 2 (deep dive) are now deterministic -- no LLM needed
    ai_model_onboarding_pass3: str = "anthropic/claude-sonnet-4"
    ai_model_onboarding_pass3_progressive: str = "anthropic/claude-sonnet-4"
    ai_model_onboarding_pass3_5: str = "anthropic/claude-sonnet-4"
    ai_model_onboarding_pass4: str = "anthropic/claude-haiku-4.5"
    # Agentic review loop (new 3a/b/c pipeline).
    # Kimi 2.5 chosen for the explorer: proven 100% tool validity at 15+ call
    # chains in the Phase 0 bakeoff, much better token efficiency than Sonnet,
    # and already used by issue_analyst / risk_detective / progress_tracker.
    # Audit + group stay on Kimi for consistency across the pipeline.
    ai_model_onboarding_pass3a_explore: str = "moonshotai/kimi-k2.5"
    ai_model_onboarding_pass3b_audit: str = "moonshotai/kimi-k2.5"
    ai_model_onboarding_pass3c_group: str = "moonshotai/kimi-k2.5"
    # Feature flag: set False to force legacy single-call flow
    onboarding_use_agentic_review: bool = True
    ai_model_issue_analyst: str = "anthropic/claude-sonnet-4"
    ai_model_progress_tracker: str = "anthropic/claude-sonnet-4"
    ai_model_pr_reviewer: str = "anthropic/claude-sonnet-4"
    ai_model_risk_detective: str = "anthropic/claude-opus-4.5"

    # --- Per-Tool Model Overrides ---
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

    # --- Reconciliation ---
    reconciliation_interval_hours: int = 6

    # --- Cost Tracking (USD per 1M tokens via OpenRouter, per model) ---
    ai_cost_per_million_input: float = 3.0    # fallback if model not in pricing table
    ai_cost_per_million_output: float = 15.0

    @property
    def model_pricing(self) -> dict:
        """Per-model pricing: {model_id: (input_per_M, output_per_M)}"""
        return {
            "anthropic/claude-haiku-4.5": (0.80, 4.0),
            "anthropic/claude-sonnet-4": (3.0, 15.0),
            "anthropic/claude-sonnet-4.5": (3.0, 15.0),
            "anthropic/claude-opus-4.5": (15.0, 75.0),
        }

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
