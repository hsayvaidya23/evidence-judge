"""`evidence-judge session judge|inspect`: grade or summarise a coding-agent session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from textwrap import indent
from typing import Annotated

import typer
from rich.text import Text

from evidence_judge._cli_common import (
    IN_FILE,
    OUT_FILE,
    OUTCOME_EXIT,
    CliError,
    Debug,
    ExitCode,
    LangfuseTraceId,
    LlmOption,
    Otel,
    check_exporters,
    chosen_llm,
    fail,
    internal,
    load_claims,
    load_redactor,
    load_settings,
    stderr,
    write_output,
)
from evidence_judge._version import __version__
from evidence_judge.core.spec_errors import SpecError, load_yaml
from evidence_judge.core.verdict import (
    Citation,
    ExpectationResult,
    Outcome,
    RunVerdict,
    Verification,
    VerificationMethod,
)
from evidence_judge.integrations import exporters
from evidence_judge.llm.base import LLMConfigError, LLMError
from evidence_judge.report.console import StdoutConsole, print_verdict
from evidence_judge.report.json_report import write_json
from evidence_judge.sessions.html import write_session_html
from evidence_judge.sessions.ingest import load_session, locate
from evidence_judge.sessions.judge import SessionJudge, SessionJudgeOptions
from evidence_judge.sessions.models import SessionRun
from evidence_judge.sessions.predicates import SessionClaimSet, SessionSpec
from evidence_judge.sessions.redaction import redact_session
from evidence_judge.sessions.report import print_session, summarize
from evidence_judge.sessions.rerun import RerunResult, RerunSpec, RerunStatus, execute

session_app = typer.Typer(
    name="session",
    help=(
        "Grade coding-agent sessions (Claude Code, Codex, and Gemini CLI through the evidence "
        "log) against their tool-call log."
    ),
    no_args_is_help=True,
)


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
_InspectProject = Annotated[
    Path | None,
    typer.Option(
        "--project",
        help="Project directory where --claude-code latest looks (default: current directory)",
    ),
]
_RerunProject = Annotated[
    Path | None,
    typer.Option(
        "--project",
        help="Project directory the re-run copies (default: current directory)",
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
    typer.Option(help="Codex `exec --json` output or rollout .jsonl", **IN_FILE),
]
_EvidenceLog = Annotated[
    Path | None,
    typer.Option("--evidence-log", help="Evidence-recorder log (session-evidence v1)", **IN_FILE),
]
_LogKey = Annotated[
    Path | None,
    typer.Option(
        "--log-key", help="Evidence-log key (default: the hooks' state dir log-key)", **IN_FILE
    ),
]
_LogAnchor = Annotated[
    Path | None,
    typer.Option(
        "--log-anchor",
        help="Evidence-log head anchor (default: by session, in --log-key's directory, else "
        "the hooks' state dir)",
        **IN_FILE,
    ),
]


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
        raise CliError(
            "provide a session with --claude-code, --codex or --evidence-log", ExitCode.USAGE
        )
    if claude_code and codex:
        raise CliError("use either --claude-code or --codex, not both", ExitCode.USAGE)
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
        raise CliError(f"could not load the session: {e}", ExitCode.USAGE) from e


def _spec(path: Path | None) -> SessionSpec | None:
    if path is None:
        return None
    try:
        return load_yaml(path.read_text(encoding="utf-8"), SessionSpec)
    except SpecError as e:
        if any(p.location == "id" and p.type == "extra_forbidden" for p in e.problems):
            raise CliError(
                f"invalid session spec {path}: this looks like a browser task spec; use "
                "evidence-judge judge --spec",
                ExitCode.USAGE,
            ) from e
        raise CliError(
            f"invalid session spec {path}:\n{indent(str(e), '  ')}", ExitCode.USAGE
        ) from e
    except (OSError, ValueError) as e:
        raise CliError(f"invalid session spec {path}: {e}", ExitCode.USAGE) from e


def _answer(answer: str | None, answer_file: Path | None) -> str | None:
    if answer is not None and answer_file is not None:
        raise CliError("use either --answer or --answer-file, not both", ExitCode.USAGE)
    if answer_file is None:
        return answer
    try:
        return answer_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise CliError(f"cannot read answer file {answer_file}: {e}", ExitCode.USAGE) from e


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
    task: Annotated[str | None, typer.Option(help="Task given to the agent")] = None,
    answer: Annotated[str | None, typer.Option(help="Use this as the agent's final answer")] = None,
    answer_file: Annotated[
        Path | None, typer.Option(help="File with the agent's final answer", **IN_FILE)
    ] = None,
    spec: Annotated[Path | None, typer.Option(help="Session spec YAML", **IN_FILE)] = None,
    claims: Annotated[
        Path | None, typer.Option(help="Pre-extracted session claims JSON", **IN_FILE)
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Write the verdict JSON here", **OUT_FILE)
    ] = None,
    html: Annotated[
        Path | None, typer.Option(help="Write the HTML report here", **OUT_FILE)
    ] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="No LLM calls; heuristic claim extraction only")
    ] = False,
    llm_backend: LlmOption = None,
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
    debug: Debug = False,
    otel: Otel = False,
    langfuse_trace_id: LangfuseTraceId = None,
) -> None:
    """Judge a coding-agent session.

    Exit code: 0 pass, 1 fail, 2 needs review, 3 bad input, 4 internal."""
    err = stderr()
    try:
        check_exporters(otel, langfuse_trace_id)
        settings = load_settings(llm_backend)
        session_spec = _spec(spec)
        if task is not None and session_spec is not None:
            session_spec = session_spec.model_copy(update={"task": task})
        claim_set = load_claims(claims, SessionClaimSet) if claims else None
        run = _load(
            claude_code,
            project,
            codex,
            evidence_log,
            _answer(answer, answer_file),
            log_key=log_key,
            log_anchor=log_anchor,
        )
        if task is not None:
            run = run.model_copy(update={"task": task})
        rerun_result = (
            _rerun(_rerun_spec(session_spec), project, rerun_base, run.cwd, quiet, rerun_baseline)
            if rerun
            else None
        )
        llm = chosen_llm(settings, llm_backend, offline=offline)
        if no_redact:
            settings = settings.model_copy(update={"redact": False})
        redactor = load_redactor(settings, persistent=llm is not None) if settings.redact else None
        session_judge = SessionJudge(settings, llm, redactor=redactor)
        run, options, report = session_judge.redact(
            run,
            SessionJudgeOptions(
                offline=offline or llm is None,
                spec=session_spec,
                claims=claim_set,
                require_tamper_evidence=require_tamper_evidence,
                run_id=run_id,
                trusted_log=trusted_log,
                rerun=rerun_result,
                require_rerun=require_rerun,
            ),
        )
        if not quiet:
            if report is None:
                err.print(Text("redaction disabled: reports may contain secrets", style="yellow"))
            else:
                err.print(Text(report.describe(), style="dim"))
        if llm is None and not offline and not quiet:
            err.print(Text("no LLM configured; judging offline", style="yellow"))
        verdict = _run(session_judge, run, options)
        if out:
            write_output(out, lambda p: write_json(verdict, p))
        if html:
            write_output(html, lambda p: write_session_html(verdict, run, p))
        export_warnings = exporters.export(verdict, otel=otel, langfuse_trace_id=langfuse_trace_id)
    except CliError as e:
        raise fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise internal(e) from None
    if not quiet:
        print_verdict(verdict, StdoutConsole())
        for label, path in (("json", out), ("html", html)):
            if path:
                err.print(Text.assemble((f"wrote {label} ", "dim"), str(path)))
        for warning in export_warnings:
            err.print(Text(warning, style="yellow"))
    raise typer.Exit(int(OUTCOME_EXIT[verdict.outcome]))


def _rerun_spec(spec: SessionSpec | None) -> RerunSpec:
    if spec is None:
        raise CliError("--rerun needs a session spec (--spec) with a rerun: block", ExitCode.USAGE)
    if spec.rerun is None:
        raise CliError(
            "the spec has no rerun: block (session rerun and session judge --rerun need one)",
            ExitCode.USAGE,
        )
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
        stderr().print(
            Text(f"re-running {' '.join(spec.command)} on a copy of {root}", style="dim")
        )
    return execute(spec, root, base=base or spec.base, session_cwd=session_cwd, baseline=baseline)


def rerun_verdict(
    result: RerunResult, run_id: str | None = None, task: str | None = None
) -> RunVerdict:
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
        task=task,
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
        verification=Verification(
            method=VerificationMethod.RERUN, reason=result.copied or result.reason
        ),
        warnings=[*review, *(f"re-run: {n}" for n in result.notes)],
    )


@session_app.command("rerun")
def rerun_command(
    spec: Annotated[Path, typer.Option(help="Session spec YAML with a rerun: block", **IN_FILE)],
    project: _RerunProject = None,
    rerun_base: _RerunBase = None,
    rerun_baseline: _RerunBaseline = None,
    out: Annotated[
        Path | None, typer.Option(help="Write the verdict JSON here", **OUT_FILE)
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only set the exit code")] = False,
    debug: Debug = False,
) -> None:
    """Re-run the spec's tests on a clean copy of the project and judge the runner's report.

    Needs no session record. Exit code: 0 pass, 1 fail, 2 needs review, 3 bad input, 4 internal."""
    try:
        session_spec = _spec(spec)
        rerun_spec = _rerun_spec(session_spec)
        verdict = rerun_verdict(
            _rerun(rerun_spec, project, rerun_base, None, quiet, rerun_baseline),
            run_id=spec.stem,
            task=session_spec.task if session_spec else None,
        )
        if out:
            write_output(out, lambda p: write_json(verdict, p))
    except CliError as e:
        raise fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise internal(e) from None
    if not quiet:
        print_verdict(verdict, StdoutConsole(), claims=False)
        if out:
            stderr().print(Text.assemble(("wrote json ", "dim"), str(out)))
    raise typer.Exit(int(OUTCOME_EXIT[verdict.outcome]))


def _run(judge: SessionJudge, run: SessionRun, options: SessionJudgeOptions) -> RunVerdict:
    try:
        return asyncio.run(judge.judge(run, options))
    except LLMConfigError as e:
        raise CliError(f"{e} (use --offline to judge without a model)", ExitCode.INTERNAL) from e
    except LLMError as e:
        raise CliError(f"LLM error: {e}", ExitCode.INTERNAL) from e


@session_app.command("inspect")
def inspect_command(
    claude_code: _ClaudeCode = None,
    project: _InspectProject = None,
    codex: _Codex = None,
    evidence_log: _EvidenceLog = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
    no_redact: Annotated[
        bool, typer.Option("--no-redact", help="Show commands unredacted")
    ] = False,
    debug: Debug = False,
) -> None:
    """Summarise a coding-agent session (commands, edits, test runs) without judging it."""
    try:
        settings = load_settings()
        run = _load(claude_code, project, codex, evidence_log)
        if settings.redact and not no_redact:
            run, _ = redact_session(load_redactor(settings, persistent=False), run)
    except CliError as e:
        raise fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise internal(e) from None
    if as_json:
        typer.echo(json.dumps(summarize(run), indent=2, ensure_ascii=False))
    else:
        print_session(run, StdoutConsole())
