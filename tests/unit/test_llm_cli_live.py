"""One real call per installed CLI backend, on the user's own login. Deselected by default
(`-m 'not live'`); run with `uv run pytest -m live tests/unit/test_llm_cli_live.py`."""

from __future__ import annotations

import shutil
import struct
import zlib
from typing import Literal

import pytest
from pydantic import BaseModel

from evidence_judge.config import Settings
from evidence_judge.extract.llm_extractor import LLMClaimExtractor
from evidence_judge.llm.base import Image, LLMRequest, Tier
from evidence_judge.llm.budget import Budget, run_budget
from evidence_judge.llm.claude_code import ClaudeCodeClient
from evidence_judge.llm.codex import CodexClient
from evidence_judge.llm.gemini import GeminiClient

pytestmark = pytest.mark.live


def _requires(executable: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(shutil.which(executable) is None, reason=f"{executable} not on PATH")


def _settings(backend: Literal["claude-code", "codex", "gemini"]) -> Settings:
    return Settings.model_validate({"llm_backend": backend, "cache_dir": None})


def _red_pixel_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * 8 for _ in range(8))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


class Colour(BaseModel):
    colour: Literal["red", "green", "blue", "other"]


class Capital(BaseModel):
    country: str
    capital: str


@_requires("claude")
async def test_live_claude_code_reads_an_image_into_the_schema():
    client = ClaudeCodeClient(_settings("claude-code"))
    budget = Budget(0.05)
    with run_budget(budget):
        result, usage = await client.generate(
            LLMRequest(
                system="You name the dominant colour of an image.",
                prompt="Which colour fills the attached image?",
                tier=Tier.FAST,
                images=[Image(data=_red_pixel_png(), mime_type="image/png")],
                effort="low",
            ),
            Colour,
        )
    assert result.colour == "red"
    assert usage.llm_calls == 1
    assert usage.cost_usd > 0
    assert budget.spent_usd == pytest.approx(usage.cost_usd)


@_requires("codex")
async def test_live_codex_extracts_claims_with_the_strict_schema():
    extractor = LLMClaimExtractor(CodexClient(_settings("codex")))
    claims, usage = await extractor.extract(
        task="Buy the Red Kettle",
        answer="I bought the Red Kettle for $24.00. The shop confirmed order #1001.",
        reasoning=[],
    )
    assert usage.llm_calls >= 1
    assert usage.input_tokens > 0
    assert claims.extractor == "llm:codex"
    assert claims.claims, claims.coverage_note
    assert any("1001" in claim.quote or "24" in claim.quote for claim in claims.claims)


@_requires("gemini")
async def test_live_gemini_answers_in_the_schema():
    result, usage = await GeminiClient(_settings("gemini")).generate(
        LLMRequest(system="You answer geography questions.", prompt="Capital of France?"),
        Capital,
    )
    assert result.capital.lower() == "paris"
    assert usage.llm_calls >= 1
