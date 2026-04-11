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

    @field_validator("write_mode")
    @classmethod
    def _validate_write_mode(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in _VALID_WRITE_MODES:
            raise ValueError(
                f"WRITE_MODE must be one of {sorted(_VALID_WRITE_MODES)}, "
                f"got {v!r}"
            )
        return normalized

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "")


settings = Settings()
