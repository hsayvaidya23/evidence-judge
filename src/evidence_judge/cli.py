from __future__ import annotations

import asyncio
import json
import zipfile
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.text import Text
from typer._click.core import Context  # pyright: ignore[reportPrivateUsage]
from typer._click.exceptions import UsageError  # pyright: ignore[reportPrivateUsage]
from typer.core import TyperGroup

from evidence_judge import __version__, engine, ingest
from evidence_judge.config import Settings, stable_redaction_key
from evidence_judge.core.claims import ClaimSet
from evidence_judge.core.evidence import Run
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict
from evidence_judge.integrations import exporters
from evidence_judge.llm import factory
from evidence_judge.llm.base import LLMClient, LLMError
from evidence_judge.redact import RedactionReport, Redactor
from evidence_judge.report.console import (
    print_evidence_summary,
    print_verdict,
    summarize_evidence,
)
from evidence_judge.report.html_report import write_html
from evidence_judge.report.json_report import export_schema, schema_to_json, write_json


class ExitCode(IntEnum):
    PASS = 0
    FAIL = 1
    NEEDS_REVIEW = 2
    USAGE = 3
    INTERNAL = 4


_OUTCOME_EXIT = {
    Outcome.PASS: ExitCode.PASS,
    Outcome.FAIL: ExitCode.FAIL,
    Outcome.NEEDS_REVIEW: ExitCode.NEEDS_REVIEW,
}
_INPUT_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError, zipfile.BadZipFile)


class CliError(Exception):
    def __init__(self, message: str, code: ExitCode) -> None:
        super().__init__(message)
        self.code = code


class InputUsageError(UsageError):
    exit_code = ExitCode.USAGE


def _remap(e: UsageError) -> InputUsageError:
    return InputUsageError(e.format_message(), e.ctx)


class _Group(TyperGroup):
    """Reports usage errors with exit code 3 so that 2 stays reserved for NEEDS_REVIEW."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: Context | None = None,
        **extra: Any,
    ) -> Context:
        try:
            return super().make_context(info_name, args, parent=parent, **extra)
        except InputUsageError:
            raise
        except UsageError as e:
            raise _remap(e) from e

    def invoke(self, ctx: Context) -> Any:
        try:
            return super().invoke(ctx)
        except InputUsageError:
            raise
        except UsageError as e:
            raise _remap(e) from e


app = typer.Typer(
    cls=_Group,
    name="evidence-judge",
    help="Grade browser-agent runs by checking every claim against recorded evidence.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _load_run(**kwargs: Any) -> Run:
    try:
        return ingest.load_run(**kwargs)
    except _INPUT_ERRORS as e:
        raise CliError(f"could not load evidence: {e}", ExitCode.USAGE) from e


def _build_llm(settings: Settings) -> LLMClient | None:
    try:
        return factory.build_llm(settings)
    except LLMError as e:
        raise CliError(f"could not initialise the LLM client: {e}", ExitCode.INTERNAL) from e


def _run_judge(
    settings: Settings,
    llm: LLMClient | None,
    run: Run,
    *,
    offline: bool,
    spec: TaskSpec | None,
    claims: ClaimSet | None,
) -> RunVerdict:
    options = engine.JudgeOptions(offline=offline, spec=spec, claims=claims)
    try:
        return asyncio.run(engine.Judge(settings, llm).judge(run, options))
    except LLMError as e:
        raise CliError(f"LLM error: {e}", ExitCode.INTERNAL) from e


def _stderr() -> Console:
    return Console(stderr=True, highlight=False)


def _fail(e: CliError) -> typer.Exit:
    _stderr().print(Text.assemble(("error: ", "bold red"), str(e)))
    return typer.Exit(int(e.code))


def _redactor(settings: Settings) -> Redactor:
    try:
        key = stable_redaction_key(settings)
    except (OSError, ValueError) as e:
        raise CliError(f"redaction key: {e}", ExitCode.USAGE) from e
    return Redactor(key) if key is not None else Redactor.from_settings(settings)


def _settings() -> Settings:
    try:
        return Settings()
    except ValidationError as e:
        raise CliError(f"invalid settings: {e}", ExitCode.USAGE) from e


def _load_spec(path: Path) -> TaskSpec:
    try:
        return TaskSpec.load(path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise CliError(f"invalid task spec {path}: {e}", ExitCode.USAGE) from e


def _load_claims(path: Path) -> ClaimSet:
    try:
        return ClaimSet.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as e:
        raise CliError(f"invalid claims file {path}: {e}", ExitCode.USAGE) from e


def _describe(report: RedactionReport) -> str:
    if not report.total:
        return "redaction: nothing sensitive found"
    parts = ", ".join(f"{k} {v}" for k, v in sorted(report.counts.items()))
    return f"redacted {report.total} values ({parts})"


_FileOpt = dict[str, Any]
_IN_FILE: _FileOpt = {"exists": True, "dir_okay": False, "readable": True, "resolve_path": True}
_IN_ANY: _FileOpt = {"exists": True, "readable": True, "resolve_path": True}
_OUT_FILE: _FileOpt = {"dir_okay": False, "writable": True, "resolve_path": True}


@app.command()
def judge(
    trace: Annotated[Path | None, typer.Option(help="Playwright trace .zip", **_IN_FILE)] = None,
    har: Annotated[Path | None, typer.Option(help="HAR file", **_IN_FILE)] = None,
    browser_use: Annotated[
        Path | None,
        typer.Option("--browser-use", help="browser-use history JSON or run directory", **_IN_ANY),
    ] = None,
    task: Annotated[str | None, typer.Option(help="Task given to the agent")] = None,
    answer: Annotated[str | None, typer.Option(help="The agent's final answer")] = None,
    answer_file: Annotated[
        Path | None, typer.Option(help="File with the agent's final answer", **_IN_FILE)
    ] = None,
    reasoning_file: Annotated[
        Path | None, typer.Option(help="File with the agent's reasoning", **_IN_FILE)
    ] = None,
    spec: Annotated[Path | None, typer.Option(help="Task spec YAML", **_IN_FILE)] = None,
    claims: Annotated[
        Path | None, typer.Option(help="Pre-extracted ClaimSet JSON", **_IN_FILE)
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Write the verdict JSON here", **_OUT_FILE)
    ] = None,
    html: Annotated[
        Path | None, typer.Option(help="Write the HTML report here", **_OUT_FILE)
    ] = None,
    offline: Annotated[
        bool, typer.Option(help="No LLM calls; heuristic claim extraction only")
    ] = False,
    no_redact: Annotated[
        bool, typer.Option("--no-redact", help="Keep secrets and PII in evidence and reports")
    ] = False,
    run_id: Annotated[str | None, typer.Option(help="Override the run id")] = None,
    allow_mismatch: Annotated[
        bool,
        typer.Option(
            "--allow-mismatch", help="Accept a HAR that does not appear to match the trace"
        ),
    ] = False,
    screenshot_root: Annotated[
        Path | None,
        typer.Option(
            help="Directory browser-use screenshots may be read from (default: next to the history)"
        ),
    ] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only set the exit code")] = False,
    debug: Annotated[bool, typer.Option(help="Show tracebacks for internal errors")] = False,
    otel: Annotated[
        bool,
        typer.Option(
            "--otel", help="Emit gen_ai.evaluation.result events via OpenTelemetry (OTEL_* env)"
        ),
    ] = False,
    langfuse_trace_id: Annotated[
        str | None, typer.Option(help="Push scores to this Langfuse trace (LANGFUSE_* env)")
    ] = None,
) -> None:
    """Judge one agent run. Exit code: 0 pass, 1 fail, 2 needs review, 3 bad input, 4 internal."""
    if not (trace or har or browser_use):
        raise InputUsageError("provide evidence with --trace, --har and/or --browser-use")
    if answer is not None and answer_file is not None:
        raise InputUsageError("use either --answer or --answer-file, not both")

    err = _stderr()
    try:
        try:
            exporters.check(otel=otel, langfuse_trace_id=langfuse_trace_id)
        except (ImportError, ValueError) as e:
            raise CliError(str(e), ExitCode.USAGE) from e
        settings = _settings()
        task_spec = _load_spec(spec) if spec else None
        claim_set = _load_claims(claims) if claims else None
        run = _load_run(
            trace=trace,
            har=har,
            browser_use=browser_use,
            answer=answer,
            answer_file=answer_file,
            reasoning_file=reasoning_file,
            task=task or (task_spec.task if task_spec else None),
            run_id=run_id,
            max_body_chars=settings.max_body_chars,
            allow_mismatch=allow_mismatch,
            screenshot_root=screenshot_root,
        )

        if settings.redact and not no_redact:
            redactor = _redactor(settings)
            run, report = redactor.redact_run(run)
            if task_spec is not None:
                task_spec, _ = redactor.redact_model(task_spec)
            if claim_set is not None:
                claim_set, _ = redactor.redact_model(claim_set)
            if not quiet:
                err.print(Text(_describe(report), style="dim"))
        elif not quiet:
            err.print(Text("redaction disabled: reports may contain secrets", style="yellow"))

        llm = None if offline else _build_llm(settings)
        if llm is None and not offline and not quiet:
            err.print(Text("no LLM configured; judging offline", style="yellow"))
        verdict = _run_judge(
            settings, llm, run, offline=offline or llm is None, spec=task_spec, claims=claim_set
        )

        if out:
            write_json(verdict, out)
        if html:
            write_html(verdict, run, html)
        export_warnings = exporters.export(verdict, otel=otel, langfuse_trace_id=langfuse_trace_id)
    except CliError as e:
        raise _fail(e) from None
    except Exception as e:
        if debug:
            raise
        _stderr().print(
            Text.assemble(
                ("internal error: ", "bold red"),
                f"{type(e).__name__}: {e}",
                ("  (re-run with --debug for a traceback)", "dim"),
            )
        )
        raise typer.Exit(int(ExitCode.INTERNAL)) from None

    if not quiet:
        print_verdict(verdict, Console(highlight=False))
        for label, path in (("json", out), ("html", html)):
            if path:
                err.print(Text.assemble((f"wrote {label} ", "dim"), str(path)))
        for warning in export_warnings:
            err.print(Text(warning, style="yellow"))
    raise typer.Exit(int(_OUTCOME_EXIT[verdict.outcome]))


@app.command()
def inspect(
    trace: Annotated[Path | None, typer.Option(help="Playwright trace .zip", **_IN_FILE)] = None,
    har: Annotated[Path | None, typer.Option(help="HAR file", **_IN_FILE)] = None,
    browser_use: Annotated[
        Path | None,
        typer.Option("--browser-use", help="browser-use history JSON or run directory", **_IN_ANY),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
    no_redact: Annotated[bool, typer.Option("--no-redact", help="Show raw URLs")] = False,
) -> None:
    """Summarise the evidence in a trace, HAR or browser-use run without judging it."""
    if not (trace or har or browser_use):
        raise InputUsageError("provide evidence with --trace, --har and/or --browser-use")
    try:
        settings = _settings()
        run = _load_run(
            trace=trace, har=har, browser_use=browser_use, max_body_chars=settings.max_body_chars
        )
    except CliError as e:
        raise _fail(e) from None
    if settings.redact and not no_redact:
        run, _ = _redactor(settings).redact_run(run)
    summary = summarize_evidence(run)
    if as_json:
        typer.echo(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    else:
        print_evidence_summary(summary, Console(highlight=False))


@app.command()
def schema(
    out: Annotated[
        Path | None,
        typer.Option(help="Write the schema to this file instead of stdout", **_OUT_FILE),
    ] = None,
) -> None:
    """Print the JSON Schema of the verdict format."""
    if out:
        export_schema(out)
        _stderr().print(Text.assemble(("wrote ", "dim"), str(out)))
    else:
        typer.echo(schema_to_json(), nl=False)


@app.command()
def version() -> None:
    """Print the evidence-judge version."""
    typer.echo(f"evidence-judge {__version__}")


@app.command()
def bench(
    runs_dir: Annotated[
        Path,
        typer.Argument(
            help="Directory of labelled runs, one sub-directory per scenario",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    seeds: Annotated[int, typer.Option(min=1, help="Forgeries per failed run and kind")] = 3,
    out: Annotated[
        Path | None, typer.Option(help="Write the results JSON here", **_OUT_FILE)
    ] = None,
    markdown: Annotated[
        Path | None, typer.Option(help="Write the Markdown report here", **_OUT_FILE)
    ] = None,
    with_llm_baseline: Annotated[
        bool,
        typer.Option("--with-llm-baseline", help="Also judge with a transcript LLM judge"),
    ] = False,
    baseline_screenshot: Annotated[
        bool,
        typer.Option("--baseline-screenshot", help="Show the baseline judge the final screenshot"),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline/--online", help="Evidence Judge without (default) or with LLM extraction"
        ),
    ] = True,
    no_redact: Annotated[
        bool, typer.Option("--no-redact", help="Judge unredacted evidence and agent text")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Do not print the report")] = False,
    debug: Annotated[bool, typer.Option(help="Show tracebacks for internal errors")] = False,
) -> None:
    """Benchmark judges on forged runs: false-PASS rates and accuracy on honest controls."""
    from evidence_judge.baselines import TranscriptLLMJudge
    from evidence_judge.bench import (
        BenchConfig,
        BenchmarkRunner,
        llm_calls_upper_bound,
        load_scenarios,
        to_markdown,
        write_json,
        write_markdown,
    )
    from evidence_judge.llm.budget import Budget

    try:
        settings = _settings()
        try:
            scenarios = load_scenarios(runs_dir, max_body_chars=settings.max_body_chars)
        except ValueError as e:
            raise CliError(str(e), ExitCode.USAGE) from e
        config = BenchConfig(seeds=seeds, offline=offline)
        llm: LLMClient | None = None
        calls = llm_calls_upper_bound(scenarios, config, baseline=with_llm_baseline)
        if calls:
            try:
                llm = factory.build_llm(settings, budget=Budget(settings.max_cost_usd * calls))
            except LLMError as e:
                raise CliError(
                    f"could not initialise the LLM client: {e}", ExitCode.INTERNAL
                ) from e
            if llm is None:
                raise CliError(
                    "--with-llm-baseline and --online need an LLM; set ANTHROPIC_API_KEY",
                    ExitCode.USAGE,
                )
        runner = BenchmarkRunner(
            settings,
            config,
            llm=llm,
            baseline=(
                TranscriptLLMJudge(llm, include_screenshot=baseline_screenshot)
                if with_llm_baseline and llm is not None
                else None
            ),
            redactor=_redactor(settings) if settings.redact and not no_redact else None,
        )
        result = asyncio.run(runner.run(scenarios))
        if out:
            write_json(result, out)
        if markdown:
            write_markdown(result, markdown)
    except CliError as e:
        raise _fail(e) from None
    except Exception as e:
        if debug:
            raise
        _stderr().print(
            Text.assemble(
                ("internal error: ", "bold red"),
                f"{type(e).__name__}: {e}",
                ("  (re-run with --debug for a traceback)", "dim"),
            )
        )
        raise typer.Exit(int(ExitCode.INTERNAL)) from None

    if not quiet:
        typer.echo(to_markdown(result), nl=False)
        for label, path in (("json", out), ("markdown", markdown)):
            if path:
                _stderr().print(Text.assemble((f"wrote {label} ", "dim"), str(path)))
