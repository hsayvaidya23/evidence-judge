"""`evidence-judge session judge|inspect`: grade or summarise a coding-agent session."""

from __future__ import annotations

import asyncio
import json
from enum import IntEnum
from pathlib import Path
from textwrap import indent
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.text import Text

from evidence_judge import __version__
from evidence_judge.config import Settings, stable_redaction_key
from evidence_judge.core.spec_errors import SpecError, load_yaml
from evidence_judge.core.verdict import (
    Citation,
    ExpectationResult,
    Outcome,
    RunVerdict,
    Verification,
    VerificationMethod,
)
from evidence_judge.llm import factory
from evidence_judge.llm.base import LLMClient, LLMConfigError, LLMError
from evidence_judge.redact import Redactor
from evidence_judge.report.console import StdoutConsole, print_verdict
from evidence_judge.report.json_report import write_json
from evidence_judge.sessions.ingest import load_session, locate
from evidence_judge.sessions.judge import SessionJudge, SessionJudgeOptions
from evidence_judge.sessions.models import SessionRun
from evidence_judge.sessions.predicates import SessionClaimSet, SessionSpec
from evidence_judge.sessions.redaction import redact_claims, redact_session, redact_spec
from evidence_judge.sessions.report import print_session, summarize
from evidence_judge.sessions.rerun import RerunResult, RerunSpec, RerunStatus, execute


class _Exit(IntEnum):
    """The same exit codes as `evidence-judge judge`."""

    PASS = 0
    FAIL = 1
    NEEDS_REVIEW = 2
    USAGE = 3
    INTERNAL = 4


_OUTCOME_EXIT = {
    Outcome.PASS: _Exit.PASS,
    Outcome.FAIL: _Exit.FAIL,
    Outcome.NEEDS_REVIEW: _Exit.NEEDS_REVIEW,
}

session_app = typer.Typer(
    name="session",
    help="Grade coding-agent sessions (Claude Code, Codex) against their tool-call log.",
    no_args_is_help=True,
)


class _Error(Exception):
    def __init__(self, message: str, code: _Exit) -> None:
        super().__init__(message)
        self.code = code


_IN_FILE: dict[str, Any] = {
    "exists": True,
    "dir_okay": False,
    "readable": True,
    "resolve_path": True,
}
_OUT_FILE: dict[str, Any] = {"dir_okay": False, "writable": True, "resolve_path": True}

_ClaudeCode = Annotated[
    str | None,
    typer.Option(
        "--claude-code",
        help="Claude Code transcript: a .jsonl path, a session id, or 'latest' for --project",
    ),
]
_Project = Annotated[
    Path | None,
    typer.Option(
        help="Project directory: where --claude-code latest looks, and the tree --rerun copies "
        "(default: current directory)"
    ),
]
_RerunBase = Annotated[
    str | None,
    typer.Option(
        "--rerun-base",
        help="Revision to compare runner configuration with (default: the spec's "
        "rerun.base, else HEAD)",
    ),
]
_RerunBaseline = Annotated[
    Path | None,
    typer.Option(
        "--rerun-baseline",
        help="Fingerprint of .venv, venv and node_modules saved when the turn started (the "
        "turn-start hook writes it); the re-run needs review when they changed since",
        dir_okay=False,
        resolve_path=True,
    ),
]
_Codex = Annotated[
    Path | None,
    typer.Option(help="Codex `exec --json` output or rollout .jsonl", **_IN_FILE),
]
_EvidenceLog = Annotated[
    Path | None,
    typer.Option("--evidence-log", help="Evidence-recorder log (session-evidence v1)", **_IN_FILE),
]
_LogKey = Annotated[
    Path | None,
    typer.Option(
        "--log-key", help="Evidence-log key (default: the hooks' state dir log-key)", **_IN_FILE
    ),
]
_LogAnchor = Annotated[
    Path | None,
    typer.Option(
        "--log-anchor",
        help="Evidence-log head anchor (default: the hooks' state dir, by session)",
        **_IN_FILE,
    ),
]
_Debug = Annotated[bool, typer.Option(help="Show tracebacks for internal errors")]


def _stderr() -> Console:
    return Console(stderr=True, highlight=False)


def _load(
    claude_code: str | None,
    project: Path | None,
    codex: Path | None,
    evidence_log: Path | None,
    answer: str | None = None,
    log_key: Path | None = None,
    log_anchor: Path | None = None,
) -> SessionRun:
    if not (claude_code or codex or evidence_log):
        raise _Error("provide a session with --claude-code, --codex or --evidence-log", _Exit.USAGE)
    if claude_code and codex:
        raise _Error("use either --claude-code or --codex, not both", _Exit.USAGE)
    try:
        transcript = locate(claude_code, project=project) if claude_code else None
        return load_session(
            claude_code=transcript,
            codex=codex,
            evidence_log=evidence_log,
            answer=answer,
            log_key=log_key,
            log_anchor=log_anchor,
        )
    except (OSError, ValueError) as e:
        raise _Error(f"could not load the session: {e}", _Exit.USAGE) from e


def _settings() -> Settings:
    try:
        return Settings()
    except ValidationError as e:
        raise _Error(f"invalid settings: {e}", _Exit.USAGE) from e


def _redactor(settings: Settings, *, persistent: bool) -> Redactor:
    if persistent and settings.redaction_key is None:
        try:
            key = stable_redaction_key(settings)
        except (OSError, ValueError):
            key = None
        if key is not None:
            return Redactor(key)
    return Redactor.from_settings(settings)


def _spec(path: Path | None) -> SessionSpec | None:
    if path is None:
        return None
    try:
        return load_yaml(path.read_text(encoding="utf-8"), SessionSpec)
    except SpecError as e:
        raise _Error(f"invalid session spec {path}:\n{indent(str(e), '  ')}", _Exit.USAGE) from e
    except (OSError, ValueError) as e:
        raise _Error(f"invalid session spec {path}: {e}", _Exit.USAGE) from e


def _claims(path: Path | None) -> SessionClaimSet | None:
    if path is None:
        return None
    try:
        return SessionClaimSet.model_validate_json(path.read_bytes())
    except OSError as e:
        raise _Error(f"cannot read claims file {path}: {e.strerror}", _Exit.USAGE) from e
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['type']}"
            for err in e.errors(include_url=False, include_input=False, include_context=False)
        )
        raise _Error(f"invalid claims file {path}: {problems}", _Exit.USAGE) from e


def _answer(answer: str | None, answer_file: Path | None) -> str | None:
    if answer is not None and answer_file is not None:
        raise _Error("use either --answer or --answer-file, not both", _Exit.USAGE)
    if answer_file is None:
        return answer
    try:
        return answer_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise _Error(f"cannot read answer file {answer_file}: {e}", _Exit.USAGE) from e


def _llm(settings: Settings, offline: bool) -> LLMClient | None:
    if offline:
        return None
    try:
        return factory.build_llm(settings)
    except LLMError as e:
        raise _Error(f"could not initialise the LLM client: {e}", _Exit.INTERNAL) from e


def _fail(e: _Error) -> typer.Exit:
    _stderr().print(Text.assemble(("error: ", "bold red"), str(e)))
    return typer.Exit(int(e.code))


def _internal(e: Exception) -> typer.Exit:
    _stderr().print(
        Text.assemble(
            ("internal error: ", "bold red"),
            f"{type(e).__name__}: {e}",
            ("  (re-run with --debug for a traceback)", "dim"),
        )
    )
    return typer.Exit(int(_Exit.INTERNAL))


@session_app.command("judge")
def judge_command(
    claude_code: _ClaudeCode = None,
    project: _Project = None,
    codex: _Codex = None,
    evidence_log: _EvidenceLog = None,
    log_key: _LogKey = None,
    log_anchor: _LogAnchor = None,
    trusted_log: Annotated[
        bool,
        typer.Option(
            "--trusted-log",
            help="The evidence log, its key and anchor were out of the agent's reach (sandbox, "
            "container, another OS user); only then is a verified log full tamper evidence",
        ),
    ] = False,
    answer: Annotated[str | None, typer.Option(help="Use this as the agent's final answer")] = None,
    answer_file: Annotated[
        Path | None, typer.Option(help="File with the agent's final answer", **_IN_FILE)
    ] = None,
    spec: Annotated[Path | None, typer.Option(help="Session spec YAML", **_IN_FILE)] = None,
    claims: Annotated[
        Path | None, typer.Option(help="Pre-extracted session claims JSON", **_IN_FILE)
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Write the verdict JSON here", **_OUT_FILE)
    ] = None,
    offline: Annotated[
        bool, typer.Option(help="No LLM calls; heuristic claim extraction only")
    ] = False,
    no_redact: Annotated[
        bool, typer.Option("--no-redact", help="Keep secrets in commands, outputs and reports")
    ] = False,
    require_tamper_evidence: Annotated[
        bool,
        typer.Option(
            "--require-tamper-evidence",
            help="Cap the run at needs review unless it comes from an evidence log or a "
            "harness stream",
        ),
    ] = False,
    rerun: Annotated[
        bool,
        typer.Option(
            "--rerun",
            help="Run the spec's rerun command on a copy of the project, in a clean "
            "environment, and decide test results from the runner's own report",
        ),
    ] = False,
    require_rerun: Annotated[
        bool,
        typer.Option(
            "--require-rerun", help="Refuse a pass unless a re-run decided the suite's result"
        ),
    ] = False,
    rerun_base: _RerunBase = None,
    rerun_baseline: _RerunBaseline = None,
    run_id: Annotated[str | None, typer.Option(help="Override the run id")] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only set the exit code")] = False,
    debug: _Debug = False,
) -> None:
    """Judge a coding-agent session. Exit code: 0 pass, 1 fail, 2 needs review, 3 bad input,
    4 internal."""
    err = _stderr()
    try:
        settings = _settings()
        session_spec = _spec(spec)
        claim_set = _claims(claims)
        run = _load(
            claude_code,
            project,
            codex,
            evidence_log,
            _answer(answer, answer_file),
            log_key=log_key,
            log_anchor=log_anchor,
        )
        rerun_result = (
            _rerun(_rerun_spec(session_spec), project, rerun_base, run.cwd, quiet, rerun_baseline)
            if rerun
            else None
        )
        llm = _llm(settings, offline)
        if settings.redact and not no_redact:
            redactor = _redactor(settings, persistent=llm is not None)
            run, report = redact_session(redactor, run)
            session_spec = redact_spec(redactor, session_spec) if session_spec else None
            claim_set = redact_claims(redactor, claim_set) if claim_set else None
            if not quiet:
                err.print(Text(report.describe(), style="dim"))
        elif not quiet:
            err.print(Text("redaction disabled: reports may contain secrets", style="yellow"))
        if llm is None and not offline and not quiet:
            err.print(Text("no LLM configured; judging offline", style="yellow"))
        options = SessionJudgeOptions(
            offline=offline or llm is None,
            spec=session_spec,
            claims=claim_set,
            require_tamper_evidence=require_tamper_evidence,
            run_id=run_id,
            trusted_log=trusted_log,
            rerun=rerun_result,
            require_rerun=require_rerun,
        )
        verdict = _run(SessionJudge(llm, max_cost_usd=settings.max_cost_usd), run, options)
        if out:
            try:
                write_json(verdict, out)
            except OSError as e:
                raise _Error(f"cannot write {out}: {e.strerror or e}", _Exit.USAGE) from e
    except _Error as e:
        raise _fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise _internal(e) from None
    if not quiet:
        print_verdict(verdict, StdoutConsole())
        if out:
            err.print(Text.assemble(("wrote json ", "dim"), str(out)))
    raise typer.Exit(int(_OUTCOME_EXIT[verdict.outcome]))


def _rerun_spec(spec: SessionSpec | None) -> RerunSpec:
    if spec is None or spec.rerun is None:
        raise _Error("--rerun needs a session spec (--spec) with a rerun: block", _Exit.USAGE)
    return spec.rerun


def _rerun(
    spec: RerunSpec,
    project: Path | None,
    base: str | None,
    session_cwd: str | None,
    quiet: bool,
    baseline: Path | None = None,
) -> RerunResult:
    root = (project or Path.cwd()).resolve()
    if not quiet:
        _stderr().print(
            Text(f"re-running {' '.join(spec.command)} on a copy of {root}", style="dim")
        )
    return execute(spec, root, base=base or spec.base, session_cwd=session_cwd, baseline=baseline)


def rerun_verdict(result: RerunResult, run_id: str | None = None) -> RunVerdict:
    """A verdict for a re-run on its own, without a session record: the suite passes, fails,
    or cannot be decided."""
    review = [f"runner configuration changed: {c}" for c in result.config_changes]
    review.extend(f"re-run: {p}" for p in result.problems)
    met = {RerunStatus.PASS: True, RerunStatus.FAIL: False}.get(result.status)
    if met is False:
        outcome = Outcome.FAIL
    elif met is True and not review:
        outcome = Outcome.PASS
    else:
        outcome = Outcome.NEEDS_REVIEW
    summary = result.reason + (
        f". Needs review: {review[0]}." if review and met is not False else "."
    )
    citation = Citation(evidence_id="rerun", excerpt=" ".join(result.command)[:160] or None)
    return RunVerdict(
        judge_version=__version__,
        run_id=run_id or "rerun",
        task=None,
        outcome=outcome,
        score={Outcome.PASS: 1.0, Outcome.FAIL: 0.0}.get(outcome, 0.5),
        summary=summary,
        expectations=[
            ExpectationResult(
                expectation_id="rerun",
                kind="expect",
                met=met,
                reason=result.reason,
                citations=[citation],
            )
        ],
        verification=Verification(method=VerificationMethod.RERUN, reason=result.reason),
        warnings=[*review, *(f"re-run: {n}" for n in result.notes)],
    )


@session_app.command("rerun")
def rerun_command(
    spec: Annotated[Path, typer.Option(help="Session spec YAML with a rerun: block", **_IN_FILE)],
    project: _Project = None,
    rerun_base: _RerunBase = None,
    rerun_baseline: _RerunBaseline = None,
    out: Annotated[
        Path | None, typer.Option(help="Write the verdict JSON here", **_OUT_FILE)
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only set the exit code")] = False,
    debug: _Debug = False,
) -> None:
    """Re-run the spec's test command on a copy of the project, in a clean environment, and
    judge the runner's own report, without a session record. Exit code: 0 pass, 1 fail, 2 needs
    review, 3 bad input, 4 internal."""
    try:
        rerun_spec = _rerun_spec(_spec(spec))
        verdict = rerun_verdict(
            _rerun(rerun_spec, project, rerun_base, None, quiet, rerun_baseline)
        )
        if out:
            try:
                write_json(verdict, out)
            except OSError as e:
                raise _Error(f"cannot write {out}: {e.strerror or e}", _Exit.USAGE) from e
    except _Error as e:
        raise _fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise _internal(e) from None
    if not quiet:
        print_verdict(verdict, StdoutConsole())
    raise typer.Exit(int(_OUTCOME_EXIT[verdict.outcome]))


def _run(judge: SessionJudge, run: SessionRun, options: SessionJudgeOptions) -> RunVerdict:
    try:
        return asyncio.run(judge.judge(run, options))
    except LLMConfigError as e:
        raise _Error(f"{e} (use --offline to judge without a model)", _Exit.INTERNAL) from e
    except LLMError as e:
        raise _Error(f"LLM error: {e}", _Exit.INTERNAL) from e


@session_app.command("inspect")
def inspect_command(
    claude_code: _ClaudeCode = None,
    project: _Project = None,
    codex: _Codex = None,
    evidence_log: _EvidenceLog = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
    no_redact: Annotated[
        bool, typer.Option("--no-redact", help="Show commands unredacted")
    ] = False,
    debug: _Debug = False,
) -> None:
    """Summarise a coding-agent session (commands, edits, test runs) without judging it."""
    try:
        settings = _settings()
        run = _load(claude_code, project, codex, evidence_log)
        if settings.redact and not no_redact:
            run, _ = redact_session(_redactor(settings, persistent=False), run)
    except _Error as e:
        raise _fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise _internal(e) from None
    if as_json:
        typer.echo(json.dumps(summarize(run), indent=2, ensure_ascii=False))
    else:
        print_session(run, StdoutConsole())
