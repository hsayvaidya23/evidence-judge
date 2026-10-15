from __future__ import annotations

import contextlib
import logging
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

REDACTION_KEY_FILE = "redaction.key"
_REDACTION_KEY_BYTES = 32
APP_DIR_NAME = "evidence-judge"

LLMBackend = Literal["anthropic", "claude-code", "codex", "gemini"]
LLM_BACKENDS: tuple[LLMBackend, ...] = ("anthropic", "claude-code", "codex", "gemini")


def default_cache_dir() -> Path:
    """The per-user cache directory: ``$XDG_CACHE_HOME/evidence-judge`` when set (absolute),
    else ``~/Library/Caches/evidence-judge`` on macOS, ``%LOCALAPPDATA%\\evidence-judge`` on
    Windows and ``~/.cache/evidence-judge`` elsewhere. Never relative to the working directory."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_DIR_NAME
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_DIR_NAME
    return Path.home() / ".cache" / APP_DIR_NAME


class Settings(BaseSettings):
    """Runtime settings. Read from environment variables (EVJ_*) or a .env file; never from code."""

    model_config = SettingsConfigDict(env_prefix="EVJ_", env_file=".env", extra="ignore")

    llm_backend: LLMBackend = Field(
        default="anthropic",
        description=(
            "Where LLM calls go: the Anthropic API (needs ANTHROPIC_API_KEY) or a coding-agent CLI "
            "you are logged into (claude-code, codex, gemini), run headless with its tools off"
        ),
    )
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    model_fast: str = "claude-haiku-5-5"
    model_strong: str = "claude-sonnet-5-5"
    cli_model_fast: str | None = Field(
        default=None,
        description=(
            "Model for claim extraction on a CLI backend. Unset: claude-code uses model_fast, "
            "codex and gemini use the CLI's own default model"
        ),
    )
    cli_model_strong: str | None = Field(
        default=None,
        description=(
            "Model for screenshot checks on a CLI backend. Unset: claude-code uses model_strong, "
            "codex and gemini use the CLI's own default model"
        ),
    )
    cli_timeout_s: float = Field(
        default=180.0,
        gt=0,
        description="Wall-clock limit for one CLI-backend call, including the CLI's start-up",
    )
    max_cost_usd: float = Field(default=0.25, gt=0, description="Hard per-run LLM budget")
    request_timeout_s: float = 60.0
    max_retries: int = Field(
        default=3,
        ge=0,
        description="SDK retries for 408/409/429/5xx and connection errors (backoff, retry-after)",
    )
    max_concurrent_llm_calls: int = Field(
        default=4, ge=1, description="Upper bound on LLM API requests in flight at once"
    )
    cache_dir: Path | None = Field(
        default_factory=default_cache_dir,
        description=f"LLM response cache; also holds the {REDACTION_KEY_FILE} file",
    )
    redact: bool = True
    redaction_key: SecretStr | None = Field(
        default=None,
        description=(
            "HMAC key so redacted values can still be matched. If unset and cache_dir is set, a "
            f"random per-installation key is created at <cache_dir>/{REDACTION_KEY_FILE} (mode "
            "0600) so redacted prompts, and therefore LLM cache keys, are stable across runs; "
            "with neither, a random key is used per process"
        ),
    )
    screenshots_to_llm: bool = Field(
        default=False,
        description="Send recorded screenshots, which are never redacted, to the vision model; "
        "while false, screenshot checks report no evidence",
    )
    max_body_chars: int = 20_000
    review_coverage_threshold: float = Field(default=0.6, ge=0, le=1)

    @field_validator(
        "anthropic_api_key", "redaction_key", "cli_model_fast", "cli_model_strong", mode="before"
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """`KEY=` in a .env file means "not set", not an empty value."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


def stable_redaction_key(settings: Settings) -> bytes | None:
    """The redaction HMAC key to use across processes, or None if no stable key is available.

    `EVJ_REDACTION_KEY` wins. Otherwise, when a cache directory is configured, a random key is
    read from (or atomically created at) `<cache_dir>/redaction.key` with 0600 permissions.
    None means the caller should fall back to a per-process key (cache hits only within a run).
    """
    if settings.redaction_key is not None:
        return settings.redaction_key.get_secret_value().encode("utf-8")
    if settings.cache_dir is None:
        return None
    path = settings.cache_dir / REDACTION_KEY_FILE
    if not path.exists():
        _create_key_file(path)
    return _read_key_file(path)


def _create_key_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".key")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(secrets.token_bytes(_REDACTION_KEY_BYTES).hex())
        with contextlib.suppress(FileExistsError):
            os.link(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _read_key_file(path: Path) -> bytes:
    mode = path.stat().st_mode & 0o077
    if mode:
        logger.warning("redaction key file %s is readable by other users; chmod 600 it", path)
    try:
        key = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"redaction key file {path} is corrupt; delete it to regenerate") from exc
    if len(key) != _REDACTION_KEY_BYTES:
        raise ValueError(f"redaction key file {path} is corrupt; delete it to regenerate")
    return key
