"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://johnny:johnny@localhost:5432/johnny",
        description="SQLAlchemy URL for the primary PostgreSQL database.",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Connection URL for the Redis instance (task queues, pub/sub).",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
