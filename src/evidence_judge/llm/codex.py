"""LLM backend that runs the OpenAI Codex CLI (`codex exec`) headless, on the user's Codex login.

The prompt goes over stdin (`-`), the system prompt as `developer_instructions` (a developer-role
message), screenshots as `--image` files in the call's temporary directory, and the schema as an
`--output-schema` file, which Codex sends as a strict JSON schema. `--json` prints JSONL events:
the last `agent_message` item holds the reply and `turn.completed` the token usage. Codex reports
no cost, so `Usage.cost_usd` stays 0.

Codex runs with a read-only sandbox, approvals off, web search off, the shell, patch, browser,
image, plugin, app, hook, memory and sub-agent features off, the user's config.toml and exec
rules ignored (so no MCP servers), and nothing persisted (`--ephemeral`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, cast

from evidence_judge.core.verdict import Usage
from evidence_judge.llm.base import LLMError, LLMRequest, T
from evidence_judge.llm.cli_client import (
    CLIClient,
    CLIReply,
    CLIRun,
    Invocation,
    as_int,
    json_lines,
)
from evidence_judge.llm.json_schema import portable_json_schema

_IMAGE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

DISABLED_FEATURES: tuple[str, ...] = (
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "remote_plugin",
    "multi_agent",
    "browser_use",
    "browser_use_external",
    "in_app_browser",
    "computer_use",
    "image_generation",
    "view_image",
    "hooks",
    "memories",
    "goals",
    "tool_suggest",
    "skill_search",
    "sleep_tool",
)
"""Codex features switched off with `-c features.<name>=false`. A `-c` override naming a feature
an installed Codex does not know is ignored, so the list survives Codex upgrades."""

LOCKDOWN_FLAGS: tuple[str, ...] = (
    "--sandbox",
    "read-only",
    "--skip-git-repo-check",
    "--ephemeral",
    "--ignore-user-config",
    "--ignore-rules",
    "--color",
    "never",
    "-c",
    'approval_policy="never"',
    "-c",
    'web_search="disabled"',
    *(arg for name in DISABLED_FEATURES for arg in ("-c", f"features.{name}=false")),
)


class CodexClient(CLIClient):
    name = "codex"
    executable: ClassVar[str] = "codex"
    supports_images: ClassVar[bool] = True
    login_hint: ClassVar[str] = "run `codex login`"

    def _invocation(
        self,
        request: LLMRequest,
        prompt: str,
        schema: type[T],
        model: str | None,
        workdir: Path,
        budget_usd: float,
    ) -> Invocation:
        schema_file = workdir / "output-schema.json"
        schema_file.write_text(
            json.dumps(portable_json_schema(schema, all_required=True)), encoding="utf-8"
        )
        argv = [
            self.executable,
            "exec",
            "--json",
            *LOCKDOWN_FLAGS,
            "--cd",
            str(workdir),
            "-c",
            f"developer_instructions={_toml_string(request.system)}",
            "--output-schema",
            str(schema_file),
        ]
        if model:
            argv += ["--model", model]
        if request.effort is not None:
            argv += ["-c", f"model_reasoning_effort={_toml_string(request.effort)}"]
        for index, image in enumerate(request.images):
            suffix = _IMAGE_SUFFIXES.get(image.mime_type)
            if suffix is None:
                raise LLMError(f"Unsupported image type {image.mime_type!r}")
            path = workdir / f"screenshot-{index}{suffix}"
            path.write_bytes(image.data)
            argv += ["--image", str(path)]
        argv.append("-")
        return Invocation(argv=argv, stdin=prompt.encode("utf-8"))

    def _parse(self, run: CLIRun) -> CLIReply:
        events = json_lines(run.stdout)
        failure = _failure_message(events)
        messages = [
            cast("str", item["text"])
            for event in events
            if event.get("type") == "item.completed"
            and isinstance(item := event.get("item"), dict)
            and item.get("type") == "agent_message"  # pyright: ignore[reportUnknownMemberType]
            and isinstance(item.get("text"), str)  # pyright: ignore[reportUnknownMemberType]
        ]
        if failure is not None or not messages:
            raise self._failure(run, failure)
        return CLIReply(text=messages[-1], usage=_usage(events))


def _toml_string(value: str) -> str:
    """`value` as a TOML basic string; JSON string escapes are valid TOML escapes."""
    return json.dumps(value, ensure_ascii=True)


def _failure_message(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict):
                message = cast("dict[str, Any]", error).get("message")
                if isinstance(message, str):
                    return message
            return "turn failed"
    return None


def _usage(events: list[dict[str, Any]]) -> Usage:
    usage = Usage()
    for event in events:
        if event.get("type") == "turn.completed" and isinstance(raw := event.get("usage"), dict):
            tokens = cast("dict[str, Any]", raw)
            usage.input_tokens += as_int(tokens.get("input_tokens"))
            usage.output_tokens += as_int(tokens.get("output_tokens"))
    return usage
