from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_VALID_WRITE_MODES = frozenset({"shadow", "comment", "full"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+asyncpg://github_assistant:password@localhost:5433/github_assistant",
        alias="DATABASE_URL",
    )
    log_level: str = Field(default="INFO", alias="GITA_LOG_LEVEL")

    # Confidence-gated write framework. Always defaults to "shadow" — real
    # writes require an explicit opt-in via WRITE_MODE=comment or =full in
    # the environment. See src/gita/agents/decisions.py for the full contract.
    write_mode: str = Field(default="shadow", alias="WRITE_MODE")

    # GitHub App credentials. Optional — the CLI and tests run fine in
    # shadow mode without them. Needed only when WRITE_MODE != shadow.
    github_app_id: int | None = Field(default=None, alias="GITHUB_APP_ID")
    github_app_private_key_path: str | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY_PATH"
    )

    # The App's slug (github.com/apps/<slug>). Used to recognise GITA's own
    # bot account in webhooks; unset means "ignore every bot sender".
    github_app_slug: str | None = Field(default=None, alias="GITHUB_APP_SLUG")

    # Webhook HMAC secret. Required for the FastAPI webhook receiver to
    # verify that incoming payloads actually came from GitHub.
    github_webhook_secret: str | None = Field(default=None, alias="GITHUB_WEBHOOK_SECRET")

    # Label that requests an onboarding review on an issue. Only counts
    # when applied by someone with triage access, or self-applied by an
    # owner/member/collaborator (see gita.web.dispatch).
    onboarding_label: str = Field(default="gita:onboard", alias="ONBOARDING_LABEL")

    # PR reviews wait this long before running, so a burst of pushes
    # collapses into one review of the newest commit (older jobs see
    # they've been superseded and skip without calling the LLM).
    review_debounce_seconds: float = Field(default=20.0, alias="REVIEW_DEBOUNCE_SECONDS")

    # Redis URL for the ARQ job queue. Required when running the worker.
    # Uses 127.0.0.1 instead of localhost — on Windows, localhost can
    # resolve to IPv6 (::1) while Docker only binds IPv4.
    redis_url: str = Field(default="redis://127.0.0.1:6379", alias="REDIS_URL")

    # LLM provider (OpenRouter). Optional at import time — only required
    # when an agent actually makes an LLM call. Tests use a FakeLLMClient.
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    ai_default_model: str = Field(default="moonshotai/kimi-k2.5", alias="AI_DEFAULT_MODEL")

    # OpenAI API key — used for file-level embeddings (text-embedding-3-small).
    # Optional: when absent, indexing skips embedding computation and
    # concept_view falls back to keyword-only FTS.
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")

    # Auto-test-generation post-reindex trigger. Both must be
    # true for any auto-trigger work to fire:
    #   - this env-level kill switch (default OFF for safety)
    #   - the per-repo ``Repo.auto_test_generation`` column (default OFF)
    # The cap bounds how many test_generator jobs a single reindex
    # enqueues; raise it deliberately once the loop has been observed
    # behaving on a real repo. Stays at 1 by default so a first push to
    # a fresh repo can never enqueue dozens of jobs at once.
    auto_test_gen_enabled: bool = Field(default=False, alias="AUTO_TEST_GEN_ENABLED")
    auto_test_gen_max_per_reindex: int = Field(default=1, alias="AUTO_TEST_GEN_MAX_PER_REINDEX")

    # Where generated tests are executed (gita/agents/test_generator/sandbox.py).
    #   docker — one locked-down container per run; the security boundary.
    #   local  — scrubbed subprocess; only for repositories you already trust.
    # Webhook-triggered generation refuses to run under "local".
    test_gen_sandbox: Literal["docker", "local"] = Field(default="docker", alias="TEST_GEN_SANDBOX")
    # Image must contain python + pytest + the target repo's runtime deps
    # (no network inside the sandbox). See docker/sandbox.Dockerfile.
    test_gen_sandbox_image: str = Field(
        default="gita-sandbox:latest", alias="TEST_GEN_SANDBOX_IMAGE"
    )
    test_gen_timeout_seconds: float = Field(default=90.0, alias="TEST_GEN_TIMEOUT_SECONDS")

    @field_validator("write_mode")
    @classmethod
    def _validate_write_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in _VALID_WRITE_MODES:
            raise ValueError(f"WRITE_MODE must be one of {sorted(_VALID_WRITE_MODES)}, got {v!r}")
        return normalized

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "")


settings = Settings()
