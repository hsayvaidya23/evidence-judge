"""What `evidence-judge judge|inspect|bench` and `evidence-judge session ...` share: exit codes,
error reporting, settings, option types, claims loading and the LLM client."""

from __future__ import annotations

from collections.abc import Callable
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.text import Text

from evidence_judge.config import EnvFileError, Settings, describe_settings_error, read_settings
from evidence_judge.core.spec_errors import json_syntax_problem
from evidence_judge.core.verdict import Outcome
from evidence_judge.llm import factory
from evidence_judge.llm.base import LLMClient, LLMError
from evidence_judge.redact import Redactor, redactor_for
from evidence_judge.report.console import StderrConsole


class ExitCode(IntEnum):
    PASS = 0
    FAIL = 1
    NEEDS_REVIEW = 2
    USAGE = 3
    INTERNAL = 4


OUTCOME_EXIT = {
    Outcome.PASS: ExitCode.PASS,
    Outcome.FAIL: ExitCode.FAIL,
    Outcome.NEEDS_REVIEW: ExitCode.NEEDS_REVIEW,
}


class CliError(Exception):
    def __init__(self, message: str, code: ExitCode) -> None:
        super().__init__(message)
        self.code = code


class LLMChoice(StrEnum):
    """The values of `--llm`; the same as `Settings.llm_backend`."""

    ANTHROPIC = "anthropic"
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    GEMINI = "gemini"


FileOpt = dict[str, Any]
IN_FILE: FileOpt = {"exists": True, "dir_okay": False, "readable": True, "resolve_path": True}
OUT_FILE: FileOpt = {"dir_okay": False, "writable": True, "resolve_path": True}

Debug = Annotated[bool, typer.Option("--debug", help="Show tracebacks for internal errors")]


def stderr() -> Console:
    return StderrConsole()


def fail(e: CliError) -> typer.Exit:
    stderr().print(Text.assemble(("error: ", "bold red"), str(e)))
    return typer.Exit(int(e.code))


def internal(e: Exception) -> typer.Exit:
    stderr().print(
        Text.assemble(
            ("internal error: ", "bold red"),
            f"{type(e).__name__}: {e}",
            ("  (re-run with --debug for a traceback)", "dim"),
        )
    )
    return typer.Exit(int(ExitCode.INTERNAL))


class _EnvFile:
    """The global ``--env-file`` option, set once per invocation by the app callback."""

    path: Path | None = None


def use_env_file(path: Path | None) -> None:
    _EnvFile.path = path


def chosen_env_file() -> Path | None:
    return _EnvFile.path


def load_settings(llm: LLMChoice | None = None) -> Settings:
    try:
        loaded = read_settings(_EnvFile.path)
    except ValidationError as e:
        raise CliError(f"invalid settings: {describe_settings_error(e)}", ExitCode.USAGE) from e
    except EnvFileError as e:
        raise CliError(str(e), ExitCode.USAGE) from e
    if llm is None:
        return loaded
    return loaded.model_copy(update={"llm_backend": llm.value})


def load_claims[M: BaseModel](path: Path, model: type[M]) -> M:
    """Load a claims file. Errors name the field and error type but never echo the file's
    content, which is not redacted yet."""
    try:
        data = path.read_bytes()
    except OSError as e:
        reason = e.strerror or type(e).__name__
        raise CliError(f"cannot read claims file {path}: {reason}", ExitCode.USAGE) from e
    if (problem := json_syntax_problem(data)) is not None:
        raise CliError(f"--claims: not valid JSON ({problem})", ExitCode.USAGE)
    try:
        return model.model_validate_json(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['type']}"
            for err in e.errors(include_url=False, include_input=False, include_context=False)
        )
        raise CliError(f"invalid claims file {path}: {problems}", ExitCode.USAGE) from e


def build_llm(settings: Settings) -> LLMClient | None:
    try:
        return factory.build_llm(settings)
    except LLMError as e:
        raise CliError(f"could not initialise the LLM client: {e}", ExitCode.INTERNAL) from e


def write_output(path: Path, write: Callable[[Path], object]) -> None:
    try:
        write(path)
    except OSError as e:
        reason = e.strerror or str(e)
        raise CliError(f"cannot write {path}: {reason}", ExitCode.USAGE) from e


def load_redactor(settings: Settings, *, persistent: bool) -> Redactor:
    """The run's redactor (see `redactor_for`): a corrupt key file is a usage error, and an
    unwritable cache is a warning."""

    def warn(message: str) -> None:
        stderr().print(Text(f"warning: {message}", "yellow"), soft_wrap=True)

    try:
        return redactor_for(settings, persistent=persistent, warn=warn)
    except ValueError as e:
        raise CliError(f"redaction key: {e}", ExitCode.USAGE) from e
