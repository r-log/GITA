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
