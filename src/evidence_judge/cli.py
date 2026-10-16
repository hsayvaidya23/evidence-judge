from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from textwrap import indent
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text
from typer._click.core import Context  # pyright: ignore[reportPrivateUsage]
from typer._click.exceptions import UsageError  # pyright: ignore[reportPrivateUsage]
from typer.core import TyperGroup

from evidence_judge import engine, ingest
from evidence_judge._cli_common import (
    IN_FILE,
    OUT_FILE,
    OUTCOME_EXIT,
    CliError,
    Debug,
    ExitCode,
    FileOpt,
    LLMChoice,
    build_llm,
    fail,
    internal,
    load_claims,
    load_redactor,
    load_settings,
    stderr,
    write_output,
)
from evidence_judge._extras import needs_extra
from evidence_judge._version import __version__
from evidence_judge.config import Settings
from evidence_judge.core.claims import ClaimSet
from evidence_judge.core.evidence import Run
from evidence_judge.core.site import Site
from evidence_judge.core.spec_errors import SpecError, load_yaml
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import RunVerdict
from evidence_judge.hooks.cli import hook_app
from evidence_judge.ingest import MismatchedEvidenceError
from evidence_judge.integrations import exporters
from evidence_judge.llm import factory
from evidence_judge.llm.base import LLMClient, LLMConfigError, LLMError
from evidence_judge.llm.budget import Budget
from evidence_judge.redact import RedactionReport, Redactor
from evidence_judge.report.console import (
    StdoutConsole,
    guard_std_streams,
    print_evidence_summary,
    print_verdict,
    summarize_evidence,
)
from evidence_judge.report.html_report import write_html
from evidence_judge.report.json_report import export_schema, schema_to_json, write_json
from evidence_judge.sessions.cli import session_app

_INPUT_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError, zipfile.BadZipFile)


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


class _App(typer.Typer):
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        guard_std_streams()
        return super().__call__(*args, **kwargs)


app = _App(
    cls=_Group,
    name="evidence-judge",
    help="Grade browser-agent runs and coding-agent sessions against the evidence.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"evidence-judge {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    _version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            help="Print the version and exit",
            callback=_print_version,
            is_eager=True,
        ),
    ] = None,
) -> None:
    pass


app.add_typer(hook_app)
app.add_typer(session_app, name="session")


def _load_run(**kwargs: Any) -> Run:
    try:
        return ingest.load_run(**kwargs)
    except MismatchedEvidenceError as e:
        raise CliError(
            f"could not load evidence: {e.reason} Pass --allow-mismatch to merge it anyway.",
            ExitCode.USAGE,
        ) from e
    except _INPUT_ERRORS as e:
        raise CliError(f"could not load evidence: {e}", ExitCode.USAGE) from e


def _run_judge(
    settings: Settings,
    llm: LLMClient | None,
    run: Run,
    *,
    offline: bool,
    spec: TaskSpec | None,
    claims: ClaimSet | None,
    site: str | None = None,
    require_tamper_evidence: bool = False,
) -> RunVerdict:
    options = engine.JudgeOptions(
        offline=offline,
        spec=spec,
        claims=claims,
        site=site,
        require_tamper_evidence=require_tamper_evidence,
    )
    try:
        return asyncio.run(engine.Judge(settings, llm).judge(run, options))
    except LLMConfigError as e:
        raise CliError(f"{e} (use --offline to judge without a model)", ExitCode.INTERNAL) from e
    except LLMError as e:
        raise CliError(f"LLM error: {e}", ExitCode.INTERNAL) from e


def _load_spec(path: Path) -> TaskSpec:
    try:
        return load_yaml(path.read_text(encoding="utf-8"), TaskSpec)
    except SpecError as e:
        raise CliError(f"invalid task spec {path}:\n{indent(str(e), '  ')}", ExitCode.USAGE) from e
    except (OSError, ValueError) as e:
        raise CliError(f"invalid task spec {path}: {e}", ExitCode.USAGE) from e


def _site(value: str) -> str:
    try:
        return str(Site.parse(value, what="--site"))
    except ValueError as e:
        raise CliError(str(e), ExitCode.USAGE) from e


def _require_evidence(trace: Path | None, har: Path | None, browser_use: Path | None) -> None:
    if not (trace or har or browser_use):
        raise InputUsageError("provide evidence with --trace, --har and/or --browser-use")


def _check_exporters(otel: bool, langfuse_trace_id: str | None) -> None:
    try:
        exporters.check(otel=otel, langfuse_trace_id=langfuse_trace_id)
    except (ImportError, ValueError) as e:
        raise CliError(str(e), ExitCode.USAGE) from e


def _redact_inputs(
    redactor: Redactor, run: Run, spec: TaskSpec | None, claims: ClaimSet | None
) -> tuple[Run, TaskSpec | None, ClaimSet | None, RedactionReport]:
    run, report = redactor.redact_run(run)
    if spec is not None:
        spec, _ = redactor.redact_model(spec)
    if claims is not None:
        claims, _ = redactor.redact_model(claims)
    return run, spec, claims, report


def _warn_about_model(
    err: Console, settings: Settings, llm: LLMClient | None, *, offline: bool
) -> None:
    if llm is None and not offline:
        err.print(Text("no LLM configured; judging offline", style="yellow"))
    if llm is not None and settings.screenshots_to_llm:
        err.print(
            Text(
                "screenshots: unredacted screenshots may be sent to the model provider "
                "(EVJ_SCREENSHOTS_TO_LLM=true)",
                style="yellow",
            ),
            soft_wrap=True,
        )


def _redact_for_judging(
    err: Console | None,
    settings: Settings,
    run: Run,
    spec: TaskSpec | None,
    claims: ClaimSet | None,
    *,
    redact: bool,
    persistent: bool,
) -> tuple[Run, TaskSpec | None, ClaimSet | None]:
    """Redact the inputs unless disabled, telling `err` (None when quiet) what happened."""
    if not redact:
        if err is not None:
            err.print(Text("redaction disabled: reports may contain secrets", style="yellow"))
        return run, spec, claims
    redactor = load_redactor(settings, persistent=persistent)
    run, spec, claims, report = _redact_inputs(redactor, run, spec, claims)
    if err is not None:
        err.print(Text(report.describe(), style="dim"))
    return run, spec, claims


def _write_reports(verdict: RunVerdict, run: Run, *, out: Path | None, html: Path | None) -> None:
    if out:
        write_output(out, lambda p: write_json(verdict, p))
    if html:
        write_output(html, lambda p: write_html(verdict, run, p))


def _print_written(err: Console, outputs: tuple[tuple[str, Path | None], ...]) -> None:
    for label, path in outputs:
        if path:
            err.print(Text.assemble((f"wrote {label} ", "dim"), str(path)))


_IN_ANY: FileOpt = {"exists": True, "readable": True, "resolve_path": True}

_Trace = Annotated[Path | None, typer.Option(help="Playwright trace .zip", **IN_FILE)]
_Har = Annotated[Path | None, typer.Option(help="HAR file", **IN_FILE)]
_BrowserUse = Annotated[
    Path | None,
    typer.Option("--browser-use", help="browser-use history JSON or run directory", **_IN_ANY),
]
_AllowMismatch = Annotated[
    bool,
    typer.Option("--allow-mismatch", help="Accept a HAR that does not appear to match the trace"),
]
_ScreenshotRoot = Annotated[
    Path | None,
    typer.Option(
        help="Directory browser-use screenshots may be read from (default: next to the history)"
    ),
]
_Otel = Annotated[
    bool,
    typer.Option(
        "--otel", help="Emit gen_ai.evaluation.result events via OpenTelemetry (OTEL_* env)"
    ),
]
_LangfuseTraceId = Annotated[
    str | None, typer.Option(help="Push scores to this Langfuse trace (LANGFUSE_* env)")
]


_Llm = Annotated[
    LLMChoice | None,
    typer.Option(
        "--llm",
        metavar="<backend>",
        help=(
            "LLM backend: anthropic (ANTHROPIC_API_KEY), or the claude-code, codex or gemini CLI "
            "you are logged into. Default: EVJ_LLM_BACKEND"
        ),
    ),
]


@app.command()
def judge(
    trace: _Trace = None,
    har: _Har = None,
    browser_use: _BrowserUse = None,
    task: Annotated[str | None, typer.Option(help="Task given to the agent")] = None,
    answer: Annotated[str | None, typer.Option(help="The agent's final answer")] = None,
    answer_file: Annotated[
        Path | None, typer.Option(help="File with the agent's final answer", **IN_FILE)
    ] = None,
    reasoning_file: Annotated[
        Path | None, typer.Option(help="File with the agent's reasoning", **IN_FILE)
    ] = None,
    spec: Annotated[Path | None, typer.Option(help="Task spec YAML", **IN_FILE)] = None,
    site: Annotated[
        str | None,
        typer.Option(
            help=(
                "Site the task is on, as a URL or host (a port, when given, must match; no "
                "path); overrides the spec's site. A PASS needs a named site: this flag, or "
                "the spec's site or allowed_hosts. Subdomains are not included unless listed "
                "in allowed_hosts"
            )
        ),
    ] = None,
    require_tamper_evidence: Annotated[
        bool,
        typer.Option(
            "--require-tamper-evidence",
            help=(
                "Cap the run at needs review unless its evidence can fully show tampering (a "
                "Playwright trace); HAR-only and browser-use runs cannot pass"
            ),
        ),
    ] = False,
    claims: Annotated[
        Path | None, typer.Option(help="Pre-extracted ClaimSet JSON", **IN_FILE)
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
    llm_backend: _Llm = None,
    no_redact: Annotated[
        bool, typer.Option("--no-redact", help="Keep secrets and PII in evidence and reports")
    ] = False,
    run_id: Annotated[str | None, typer.Option(help="Override the run id")] = None,
    allow_mismatch: _AllowMismatch = False,
    screenshot_root: _ScreenshotRoot = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only set the exit code")] = False,
    debug: Debug = False,
    otel: _Otel = False,
    langfuse_trace_id: _LangfuseTraceId = None,
) -> None:
    """Judge one agent run. Exit code: 0 pass, 1 fail, 2 needs review, 3 bad input, 4 internal."""
    _require_evidence(trace, har, browser_use)
    if answer is not None and answer_file is not None:
        raise InputUsageError("use either --answer or --answer-file, not both")

    err = stderr()
    try:
        _check_exporters(otel, langfuse_trace_id)
        settings = load_settings(llm_backend)
        task_spec = _load_spec(spec) if spec else None
        site_name = _site(site) if site is not None else None
        claim_set = load_claims(claims, ClaimSet) if claims else None
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

        llm = None if offline else build_llm(settings)
        run, task_spec, claim_set = _redact_for_judging(
            err if not quiet else None,
            settings,
            run,
            task_spec,
            claim_set,
            redact=settings.redact and not no_redact,
            persistent=llm is not None,
        )
        if not quiet:
            _warn_about_model(err, settings, llm, offline=offline)
        verdict = _run_judge(
            settings,
            llm,
            run,
            offline=offline or llm is None,
            spec=task_spec,
            claims=claim_set,
            site=site_name,
            require_tamper_evidence=require_tamper_evidence,
        )

        _write_reports(verdict, run, out=out, html=html)
        export_warnings = exporters.export(verdict, otel=otel, langfuse_trace_id=langfuse_trace_id)
    except CliError as e:
        raise fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise internal(e) from None

    if not quiet:
        print_verdict(verdict, StdoutConsole())
        _print_written(err, (("json", out), ("html", html)))
        for warning in export_warnings:
            err.print(Text(warning, style="yellow"))
    raise typer.Exit(int(OUTCOME_EXIT[verdict.outcome]))


@app.command()
def inspect(
    trace: _Trace = None,
    har: _Har = None,
    browser_use: _BrowserUse = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
    no_redact: Annotated[bool, typer.Option("--no-redact", help="Show raw URLs")] = False,
    debug: Debug = False,
) -> None:
    """Summarise the evidence in a trace, HAR or browser-use run without judging it."""
    _require_evidence(trace, har, browser_use)
    try:
        settings = load_settings()
        run = _load_run(
            trace=trace, har=har, browser_use=browser_use, max_body_chars=settings.max_body_chars
        )
        if settings.redact and not no_redact:
            run, _ = load_redactor(settings, persistent=False).redact_run(run)
        summary = summarize_evidence(run)
    except CliError as e:
        raise fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise internal(e) from None
    if as_json:
        typer.echo(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    else:
        print_evidence_summary(summary, StdoutConsole())


@app.command()
def schema(
    out: Annotated[
        Path | None,
        typer.Option(help="Write the schema to this file instead of stdout", **OUT_FILE),
    ] = None,
) -> None:
    """Print the JSON Schema of the verdict format."""
    if out:
        try:
            write_output(out, export_schema)
        except CliError as e:
            raise fail(e) from None
        stderr().print(Text.assemble(("wrote ", "dim"), str(out)))
    else:
        typer.echo(schema_to_json(), nl=False)


@app.command()
def version() -> None:
    """Print the evidence-judge version."""
    typer.echo(f"evidence-judge {__version__}")


@app.command("mcp")
def mcp_server(
    root: Annotated[
        list[Path] | None,
        typer.Option(
            help=(
                "Directory clients may read and write files in; repeat for more. "
                "Default: the working directory"
            ),
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    http: Annotated[
        bool,
        typer.Option("--http", help="Serve streamable HTTP on 127.0.0.1 instead of stdio"),
    ] = False,
    port: Annotated[
        int, typer.Option(min=1, max=65535, help="Port for --http (endpoint /mcp)")
    ] = 8000,
    offline_only: Annotated[
        bool,
        typer.Option("--offline-only", help="Refuse judge_run calls that ask for a model"),
    ] = False,
) -> None:
    """Run an MCP server so coding agents can judge, inspect and explain runs as tools."""
    try:
        from evidence_judge.mcp import Roots, ServerConfig, serve
    except ImportError as e:
        stderr().print(
            Text.assemble(
                ("error: ", "bold red"),
                f"the MCP server is not installed ({e}); {needs_extra('mcp')}",
            )
        )
        raise typer.Exit(int(ExitCode.USAGE)) from None
    config = ServerConfig(
        roots=Roots.of(root or [Path.cwd()]),
        allow_llm=not offline_only,
    )
    serve(config, transport="streamable-http" if http else "stdio", port=port)


def _bench_llm(settings: Settings, calls: int) -> LLMClient | None:
    """The benchmark's LLM with one budget for all ``calls``; None when no call needs a model."""
    if not calls:
        return None
    try:
        llm = factory.build_llm(settings, budget=Budget(settings.max_cost_usd * calls))
    except LLMError as e:
        raise CliError(f"could not initialise the LLM client: {e}", ExitCode.INTERNAL) from e
    if llm is None:
        raise CliError(
            "--with-llm-baseline and --online need an LLM; set ANTHROPIC_API_KEY or pass --llm",
            ExitCode.USAGE,
        )
    return llm


@app.command()
def bench(
    runs_dirs: Annotated[
        list[Path],
        typer.Argument(
            help="Directories of labelled runs, one sub-directory per scenario",
            metavar="RUNS_DIR...",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    seeds: Annotated[
        int, typer.Option(min=1, metavar="<n>", help="Forgeries per failed run and kind")
    ] = 3,
    out: Annotated[
        Path | None, typer.Option(help="Write the results JSON here", **OUT_FILE)
    ] = None,
    markdown: Annotated[
        Path | None, typer.Option(help="Write the Markdown report here", **OUT_FILE)
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
    llm_backend: _Llm = None,
    no_redact: Annotated[
        bool, typer.Option("--no-redact", help="Judge unredacted evidence and agent text")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Do not print the report")] = False,
    debug: Debug = False,
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

    try:
        settings = load_settings(llm_backend)
        try:
            scenarios = load_scenarios(*runs_dirs, max_body_chars=settings.max_body_chars)
        except ValueError as e:
            raise CliError(str(e), ExitCode.USAGE) from e
        config = BenchConfig(seeds=seeds, offline=offline)
        calls = llm_calls_upper_bound(scenarios, config, baseline=with_llm_baseline)
        llm = _bench_llm(settings, calls)
        runner = BenchmarkRunner(
            settings,
            config,
            llm=llm,
            baseline=(
                TranscriptLLMJudge(llm, include_screenshot=baseline_screenshot)
                if with_llm_baseline and llm is not None
                else None
            ),
            redactor=(
                load_redactor(settings, persistent=llm is not None)
                if settings.redact and not no_redact
                else None
            ),
        )
        result = asyncio.run(runner.run(scenarios))
        if out:
            write_output(out, lambda p: write_json(result, p))
        if markdown:
            write_output(markdown, lambda p: write_markdown(result, p))
    except CliError as e:
        raise fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise internal(e) from None

    if not quiet:
        typer.echo(to_markdown(result), nl=False)
        _print_written(stderr(), (("json", out), ("markdown", markdown)))
