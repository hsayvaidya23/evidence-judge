"""LLM backend that runs the Google Gemini CLI (`gemini`) headless, on the user's Gemini login.

Gemini CLI has no schema flag, so the JSON schema goes into the system prompt (which replaces the
CLI's own, via `GEMINI_SYSTEM_MD`) and the reply is validated, with one repair retry, like every
backend. The prompt goes over stdin; `-p` only adds a closing instruction and forces headless
mode. `--output-format json` prints one object: `response` (the model's text), `stats` (token
counts per model) and, on failure, `error`. Gemini reports no cost, so `Usage.cost_usd` stays 0.

Tools: a policy file passed with `--policy` denies every tool (`toolName = "*"`, built-in and
MCP alike), workspace settings in the temporary directory empty the built-in tool allow-list and
switch hooks and skills off, no MCP server is allowed to start, and the default
approval mode never auto-approves anything. Exit codes follow the CLI source (41 auth, 42 input,
44 sandbox, 52 config) and its docs (1 general error, 53 turn limit). Images are not sent:
Gemini CLI reads images only through its file tools, which stay off, so screenshot checks report
no evidence with this backend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, cast

from evidence_judge.core.verdict import Usage
from evidence_judge.llm.base import LLMAuthError, LLMConfigError, LLMError, LLMRequest, T
from evidence_judge.llm.cli_client import CLIClient, CLIReply, CLIRun, Invocation, as_int
from evidence_judge.llm.json_schema import portable_json_schema

EXIT_AUTH = 41
EXIT_INPUT = 42
EXIT_SANDBOX = 44
EXIT_CONFIG = 52

DENY_ALL_POLICY = """\
[[rule]]
toolName = "*"
decision = "deny"
priority = 999
denyMessage = "Tools are disabled: answer from the prompt alone."
"""

WORKSPACE_SETTINGS: dict[str, Any] = {
    "tools": {"core": []},
    "hooksConfig": {"enabled": False},
    "skills": {"enabled": False},
}
"""Written to `<temp dir>/.gemini/settings.json`; workspace settings outrank user settings, and
`--skip-trust` makes the CLI load them. An empty `tools.core` allow-list denies every built-in
tool; the user's hooks and skills stay off."""

NO_MCP_SERVER = "evidence-judge-no-mcp"
"""Passed to `--allowed-mcp-server-names`: a name no server has, so none is started."""

CLOSING_INSTRUCTION = (
    "Answer the request above. Reply with the JSON object only, with no code fence and no "
    "other text."
)


class GeminiClient(CLIClient):
    name = "gemini"
    executable: ClassVar[str] = "gemini"
    supports_images: ClassVar[bool] = False
    login_hint: ClassVar[str] = "run `gemini` once and sign in"

    def _invocation(
        self,
        request: LLMRequest,
        prompt: str,
        schema: type[T],
        model: str | None,
        workdir: Path,
        budget_usd: float,
    ) -> Invocation:
        policy = workdir / "deny-all-tools.toml"
        policy.write_text(DENY_ALL_POLICY, encoding="utf-8")
        system = workdir / "system.md"
        system.write_text(_system_prompt(request.system, schema), encoding="utf-8")
        settings_dir = workdir / ".gemini"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps(WORKSPACE_SETTINGS), encoding="utf-8"
        )
        argv = [
            self.executable,
            "--output-format",
            "json",
            "--approval-mode",
            "default",
            "--policy",
            str(policy),
            "--allowed-mcp-server-names",
            NO_MCP_SERVER,
            "--skip-trust",
        ]
        if model:
            argv += ["--model", model]
        argv += ["--prompt", CLOSING_INSTRUCTION]
        return Invocation(
            argv=argv, stdin=prompt.encode("utf-8"), env={"GEMINI_SYSTEM_MD": str(system)}
        )

    def _parse(self, run: CLIRun) -> CLIReply:
        output = _output_object(run.stdout)
        error = output.get("error") if output else None
        if run.returncode != 0 or error or output is None:
            raise self._exit_error(run, _error_message(error))
        response = output.get("response")
        return CLIReply(text=response if isinstance(response, str) else None, usage=_usage(output))

    def _exit_error(self, run: CLIRun, message: str | None) -> LLMError:
        detail = message or run.stderr.strip() or "no output"
        if run.returncode == EXIT_AUTH:
            return LLMAuthError(f"gemini could not authenticate: {detail}; {self.login_hint}")
        if run.returncode in (EXIT_INPUT, EXIT_SANDBOX, EXIT_CONFIG):
            return LLMConfigError(f"gemini refused the call (exit {run.returncode}): {detail}")
        return self._failure(run, message)


def _system_prompt(system: str, schema: type[T]) -> str:
    json_schema = json.dumps(portable_json_schema(schema, all_required=False), indent=1)
    return (
        f"{system}\n\n"
        "# Output format\n"
        "You have no tools. Reply with exactly one JSON object that validates against this JSON "
        "schema, and nothing else: no prose, no code fence.\n\n"
        f"{json_schema}\n"
    )


def _output_object(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        value = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _error_message(error: Any) -> str | None:
    if isinstance(error, dict):
        message = cast("dict[str, Any]", error).get("message")
        if isinstance(message, str):
            return message
        return json.dumps(error)
    if isinstance(error, str):
        return error
    return None


def _usage(output: dict[str, Any]) -> Usage:
    usage = Usage()
    stats = output.get("stats")
    models = cast("dict[str, Any]", stats).get("models") if isinstance(stats, dict) else None
    if not isinstance(models, dict):
        return usage
    for entry in cast("dict[str, Any]", models).values():
        tokens = cast("dict[str, Any]", entry).get("tokens") if isinstance(entry, dict) else None
        if isinstance(tokens, dict):
            counts = cast("dict[str, Any]", tokens)
            usage.input_tokens += as_int(counts.get("prompt"))
            usage.output_tokens += as_int(counts.get("candidates")) + as_int(counts.get("thoughts"))
    return usage
