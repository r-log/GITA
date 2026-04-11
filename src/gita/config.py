from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("+asyncpg", "")


settings = Settings()
