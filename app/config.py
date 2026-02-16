import sys

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # PostgreSQL
    DATABASE_URL: str = "postgresql+asyncpg://whisper:whisper@localhost:5432/whisper"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    JWT_SECRET_KEY: str = "CHANGE-ME-IN-PRODUCTION"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_DAYS: int = 7

    # Rate limiting
    LOGIN_RATE_LIMIT: int = 5  # max attempts
    LOGIN_RATE_WINDOW: int = 300  # seconds (5 min)

    # CORS
    CORS_ORIGINS: str = ""  # comma-separated origins, e.g. "https://app.example.com"

    # Environment
    ENVIRONMENT: str = "production"  # "development" or "production"

    # WebSocket auth ticket TTL
    WS_TICKET_TTL: int = 30  # seconds

    # Trusted proxy IPs that are allowed to set X-Forwarded-For
    # Comma-separated, e.g. "127.0.0.1,10.0.0.0/8"
    TRUSTED_PROXIES: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


_UNSAFE_SECRETS = {"CHANGE-ME-IN-PRODUCTION", "", "secret", "test"}


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.JWT_SECRET_KEY in _UNSAFE_SECRETS:
        print(
            "FATAL: JWT_SECRET_KEY is not set or uses an unsafe default. "
            "Set a strong random secret via the JWT_SECRET_KEY environment variable.",
            file=sys.stderr,
        )
        sys.exit(1)
    return settings
