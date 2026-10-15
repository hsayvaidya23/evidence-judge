from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class HookSettings(BaseSettings):
    """Agent-hook settings, read from ``EVJ_HOOK_*`` environment variables only."""

    model_config = SettingsConfigDict(env_prefix="EVJ_HOOK_", extra="ignore")

    enabled: bool = Field(default=True, description="Set false to turn every hook into a no-op")
    timeout_s: float = Field(
        default=60.0, gt=0, le=600, description="Wall-clock limit for one judgement"
    )
    block_on_needs_review: bool = Field(
        default=False, description="Also keep the agent working when the verdict is needs review"
    )
    on_bad_evidence: Literal["block", "notice"] = Field(
        default="block",
        description="When Playwright was used but its traces are missing, unreadable, from "
        "before the turn or rejected by the judge: keep the agent working, or only tell the user",
    )
    llm: bool = Field(
        default=False,
        description="Judge with the configured LLM (ANTHROPIC_API_KEY); offline when false",
    )
    trace_dirs: list[Path] = Field(
        default_factory=list[Path],
        description="Extra directories to look for traces in, relative to the project root",
    )
    spec: Path | None = Field(
        default=None, description="Task spec to judge with, relative to the project root"
    )
    pass_env: list[str] = Field(
        default_factory=list[str],
        description="EVJ_* variables the judge child may inherit; every other EVJ_* is dropped",
    )
    max_trace_age_s: float = Field(
        default=1800.0,
        gt=0,
        description="Newest trace age accepted when the turn's start time is unknown",
    )
