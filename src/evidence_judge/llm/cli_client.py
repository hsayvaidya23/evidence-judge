"""Shared machinery for LLM backends that drive a coding-agent CLI the user is logged into.

Each call runs the CLI once, headless, as a subprocess (no shell), in a fresh empty temporary
directory that is deleted afterwards, with the CLI's tools switched off. The prompt goes over
stdin, never on the command line. The reply is validated against the pydantic schema; an invalid
reply is retried once with the validation error appended, like the Anthropic backend.

The CLIs retry transient API errors (rate limits, overload, dropped connections) themselves, so
this layer does not retry again; a call that fails anyway raises `LLMError`. A CLI that is not
installed, not logged in, or rejects the model raises `LLMConfigError` (`LLMAuthError` for
authentication), which the judge lets propagate instead of degrading.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ValidationError

from evidence_judge.config import Settings
from evidence_judge.core.verdict import Usage
from evidence_judge.llm._repair import (
    MAX_ERROR_CHARS,
    InvalidOutputError,
    format_validation_error,
    with_error,
)
from evidence_judge.llm.base import LLMAuthError, LLMConfigError, LLMError, LLMRequest, T, Tier
from evidence_judge.llm.budget import Budget, Reservation

logger = logging.getLogger(__name__)

_DETAIL_CHARS = 400
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```$", re.S)
_AUTH_HINTS = re.compile(
    r"not logged in|/login\b|\blogin required|please (?:log ?in|sign in|authenticate)"
    r"|\b401\b|\bunauthori[sz]ed\b|invalid api key|invalid x-api-key|authentication (?:failed|error)"
    r"|\b403\b|\bforbidden\b|credentials? (?:not found|expired|invalid)|token (?:has )?expired",
    re.IGNORECASE,
)
_MODEL_HINTS = re.compile(
    r"\bmodel\b[^\n]{0,80}\b(?:not found|does not exist|not supported|is not available|"
    r"unknown|invalid|not allowed)\b|\b(?:unknown|invalid|unsupported) model\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Invocation:
    """One CLI run: the argument vector (argv[0] is replaced by the resolved executable), the
    bytes fed to stdin, and environment variables added to the inherited environment."""

    argv: list[str]
    stdin: bytes
    env: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class CLIRun:
    returncode: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class CLIReply:
    """What a CLI answered: an already-parsed JSON value (`payload`), text holding JSON, or why
    the CLI produced no valid output (`invalid`), which is repaired like a schema failure."""

    payload: Any = None
    text: str | None = None
    invalid: str | None = None
    usage: Usage = field(default_factory=Usage)


class CLIClient:
    """Base class for CLI backends. Subclasses build the invocation and parse the output."""

    name: str
    executable: ClassVar[str]
    supports_images: ClassVar[bool]
    login_hint: ClassVar[str]

    def __init__(
        self,
        settings: Settings,
        *,
        budget: Budget | None = None,
        executable: str | None = None,
    ) -> None:
        self._settings = settings
        self.budget = budget
        found = shutil.which(executable or self.executable)
        if found is None:
            raise LLMConfigError(
                f"the {self.name} backend needs the `{executable or self.executable}` CLI on "
                f"PATH; install it and {self.login_hint}"
            )
        self.path = found
        self.totals = Usage()

    def model_for(self, tier: Tier) -> str | None:
        """The model this backend asks for at `tier`; None leaves the choice to the CLI."""
        if tier is Tier.STRONG:
            return self._settings.cli_model_strong
        return self._settings.cli_model_fast

    def cache_model(self, tier: Tier) -> str:
        """The model part of the LLM cache key, distinct from other backends' entries."""
        return f"{self.name}:{self.model_for(tier) or 'default'}"

    async def generate(self, request: LLMRequest, schema: type[T]) -> tuple[T, Usage]:
        if request.images and not self.supports_images:
            raise LLMError(
                f"the {self.name} backend cannot send images to the model, so screenshot checks "
                "are unavailable with it"
            )
        usage = Usage()
        error: str | None = None
        for _attempt in range(2):
            prompt = request.prompt if error is None else with_error(request.prompt, error)
            reply = await self._call(request, prompt, schema)
            usage.add(reply.usage)
            try:
                return _validate(reply, schema), usage
            except InvalidOutputError as exc:
                error = str(exc)[:MAX_ERROR_CHARS]
                logger.debug("%s output invalid for %s, retrying once", self.name, schema.__name__)
        raise LLMError(f"Model output did not satisfy {schema.__name__} after a retry: {error}")

    async def _call(self, request: LLMRequest, prompt: str, schema: type[T]) -> CLIReply:
        model = self.model_for(request.tier)
        reservation = Reservation(
            self._estimate_usd(request, prompt, model),
            client_budget=self.budget,
            fallback_cap_usd=self._settings.max_cost_usd,
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="evidence-judge-llm-", ignore_cleanup_errors=True
            ) as tmp:
                workdir = Path(tmp)
                invocation = self._invocation(
                    request, prompt, schema, model, workdir, reservation.remaining_usd
                )
                started = time.perf_counter()
                run = await self._run(invocation, workdir)
                latency_ms = (time.perf_counter() - started) * 1000
            reply = self._parse(run)
            reply.usage.llm_calls = 1
            reply.usage.latency_ms = latency_ms
            reservation.settle(reply.usage.cost_usd)
        finally:
            reservation.release()
        self.totals.add(reply.usage)
        logger.debug(
            "%s call model=%s in=%d out=%d cost=%.6f latency_ms=%.0f",
            self.name,
            model or "default",
            reply.usage.input_tokens,
            reply.usage.output_tokens,
            reply.usage.cost_usd,
            latency_ms,
        )
        return reply

    async def _run(self, invocation: Invocation, workdir: Path) -> CLIRun:
        argv = [self.path, *invocation.argv[1:]]
        env = {**os.environ, "NO_COLOR": "1", **invocation.env}
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise LLMConfigError(f"could not start `{self.path}`: {exc}") from exc
        timeout = self._settings.cli_timeout_s
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await process.communicate(invocation.stdin)
        except TimeoutError:
            await _terminate(process)
            raise LLMError(
                f"{self.name} did not answer within {timeout:g}s (EVJ_CLI_TIMEOUT_S)"
            ) from None
        except BaseException:
            await _terminate(process)
            raise
        return CLIRun(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    def _failure(self, run: CLIRun, message: str | None = None) -> LLMError:
        """The error for a failed run: auth and model problems are configuration errors."""
        detail = _excerpt(message or run.stderr or run.stdout) or "no output"
        text = f"{message or ''}\n{run.stderr}"
        if _AUTH_HINTS.search(text):
            return LLMAuthError(
                f"{self.name} is not logged in or was refused: {detail}; {self.login_hint}"
            )
        if _MODEL_HINTS.search(text):
            return LLMConfigError(
                f"{self.name} rejected the model: {detail}; check EVJ_CLI_MODEL_FAST / "
                "EVJ_CLI_MODEL_STRONG"
            )
        return LLMError(f"{self.name} failed (exit {run.returncode}): {detail}")

    def _estimate_usd(self, request: LLMRequest, prompt: str, model: str | None) -> float:
        """Worst-case cost of one call, reserved against the budget. CLIs that report no cost
        reserve nothing; the budget still refuses calls once it is spent."""
        return 0.0

    def _invocation(
        self,
        request: LLMRequest,
        prompt: str,
        schema: type[T],
        model: str | None,
        workdir: Path,
        budget_usd: float,
    ) -> Invocation:
        raise NotImplementedError

    def _parse(self, run: CLIRun) -> CLIReply:
        raise NotImplementedError


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill the CLI and everything it started (it runs in its own session on POSIX)."""
    with contextlib.suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    with contextlib.suppress(Exception):
        await process.wait()


def _validate(reply: CLIReply, schema: type[T]) -> T:
    if reply.invalid is not None:
        raise InvalidOutputError(reply.invalid)
    if reply.payload is not None:
        try:
            return schema.model_validate(reply.payload)
        except ValidationError as exc:
            raise InvalidOutputError(format_validation_error(exc)) from exc
    text = (reply.text or "").strip()
    if not text:
        raise InvalidOutputError("No JSON output in the response")
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise InvalidOutputError(format_validation_error(exc)) from exc


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _DETAIL_CHARS else f"{flat[: _DETAIL_CHARS - 1]}…"


def json_lines(text: str) -> list[dict[str, Any]]:
    """The JSON objects in JSONL output; other lines (banners, logs) are skipped."""
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)  # pyright: ignore[reportUnknownArgumentType]
    return events


def as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
