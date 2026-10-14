from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Read from environment variables (EVJ_*) or a .env file; never from code."""

    model_config = SettingsConfigDict(env_prefix="EVJ_", env_file=".env", extra="ignore")

    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    model_fast: str = "claude-haiku-5-5"
    model_strong: str = "claude-sonnet-5-5"
    max_cost_usd: float = Field(default=0.25, gt=0, description="Hard per-run LLM budget")
    request_timeout_s: float = 60.0
    max_retries: int = 3
    cache_dir: Path | None = Field(default=Path(".evidence-judge/cache"))
    redact: bool = True
    redaction_key: SecretStr | None = Field(
        default=None,
        description="HMAC key so redacted values can still be matched; random per process if unset",
    )
    max_body_chars: int = 20_000
    review_coverage_threshold: float = Field(default=0.6, ge=0, le=1)
