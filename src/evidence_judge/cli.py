from __future__ import annotations

import asyncio
import json
import zipfile
from collections.abc import Callable
from enum import IntEnum, StrEnum
from pathlib import Path
from textwrap import indent
from typing import Annotated, Any

import typer
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
from evidence_judge.core.site import Site
from evidence_judge.core.spec_errors import SpecError, load_yaml
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict
from evidence_judge.hooks.cli import hook_app
from evidence_judge.ingest import MismatchedEvidenceError
from evidence_judge.integrations import exporters
from evidence_judge.llm import factory
from evidence_judge.llm.base import LLMClient, LLMConfigError, LLMError
from evidence_judge.llm.budget import Budget
from evidence_judge.redact import RedactionReport, Redactor
from evidence_judge.report.console import (
    print_evidence_summary,
    print_verdict,
    summarize_evidence,
)
from evidence_judge.report.html_report import write_html
from evidence_judge.report.json_report import export_schema, schema_to_json, write_json
from evidence_judge.sessions.cli import session_app


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


def _stderr() -> Console:
    return Console(stderr=True, highlight=False)


def _fail(e: CliError) -> typer.Exit:
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
    return typer.Exit(int(ExitCode.INTERNAL))


def _redactor(settings: Settings, *, persistent: bool = True) -> Redactor:
    """The run's redactor. Only a ``persistent`` one (an LLM cache will be used) reads or creates
    the key file in the cache directory; an unwritable cache falls back to a per-process key."""
    if not persistent or settings.redaction_key is not None:
        return Redactor.from_settings(settings)
    try:
        key = stable_redaction_key(settings)
    except ValueError as e:
        raise CliError(f"redaction key: {e}", ExitCode.USAGE) from e
    except OSError as e:
        _stderr().print(
            Text(f"warning: redaction key unavailable ({e}); using a per-process key", "yellow"),
            soft_wrap=True,
        )
        key = None
    return Redactor(key) if key is not None else Redactor.from_settings(settings)


def _settings(llm: LLMChoice | None = None) -> Settings:
    try:
        settings = Settings()
    except ValidationError as e:
        raise CliError(f"invalid settings: {e}", ExitCode.USAGE) from e
    if llm is None:
        return settings
    return settings.model_copy(update={"llm_backend": llm.value})


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


def _load_claims(path: Path) -> ClaimSet:
    """Load a ClaimSet. Errors name the field and error type but never echo the file's content,
    which is not redacted yet."""
    try:
        data = path.read_bytes()
    except OSError as e:
        reason = e.strerror or type(e).__name__
        raise CliError(f"cannot read claims file {path}: {reason}", ExitCode.USAGE) from e
    try:
        return ClaimSet.model_validate_json(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['type']}"
            for err in e.errors(include_url=False, include_input=False, include_context=False)
        )
        raise CliError(f"invalid claims file {path}: {problems}", ExitCode.USAGE) from e


def _write_output(path: Path, write: Callable[[Path], object]) -> None:
    try:
        write(path)
    except OSError as e:
        reason = e.strerror or str(e)
        raise CliError(f"cannot write {path}: {reason}", ExitCode.USAGE) from e


def _describe(report: RedactionReport) -> str:
    if not report.total:
        return "redaction: nothing sensitive found"
    parts = ", ".join(f"{k} {v}" for k, v in sorted(report.counts.items()))
    return f"redacted {report.total} values ({parts})"


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
    redactor = _redactor(settings, persistent=persistent)
    run, spec, claims, report = _redact_inputs(redactor, run, spec, claims)
    if err is not None:
        err.print(Text(_describe(report), style="dim"))
    return run, spec, claims


def _write_reports(verdict: RunVerdict, run: Run, *, out: Path | None, html: Path | None) -> None:
    if out:
        _write_output(out, lambda p: write_json(verdict, p))
    if html:
        _write_output(html, lambda p: write_html(verdict, run, p))


def _print_written(err: Console, outputs: tuple[tuple[str, Path | None], ...]) -> None:
    for label, path in outputs:
        if path:
            err.print(Text.assemble((f"wrote {label} ", "dim"), str(path)))


_FileOpt = dict[str, Any]
_IN_FILE: _FileOpt = {"exists": True, "dir_okay": False, "readable": True, "resolve_path": True}
_IN_ANY: _FileOpt = {"exists": True, "readable": True, "resolve_path": True}
_OUT_FILE: _FileOpt = {"dir_okay": False, "writable": True, "resolve_path": True}

_Trace = Annotated[Path | None, typer.Option(help="Playwright trace .zip", **_IN_FILE)]
_Har = Annotated[Path | None, typer.Option(help="HAR file", **_IN_FILE)]
_BrowserUse = Annotated[
    Path | None,
    typer.Option("--browser-use", help="browser-use history JSON or run directory", **_IN_ANY),
]
_Debug = Annotated[bool, typer.Option(help="Show tracebacks for internal errors")]
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


class LLMChoice(StrEnum):
    """The values of `--llm`; the same as `Settings.llm_backend`."""

    ANTHROPIC = "anthropic"
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    GEMINI = "gemini"


_Llm = Annotated[
    LLMChoice | None,
    typer.Option(
        "--llm",
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
        Path | None, typer.Option(help="File with the agent's final answer", **_IN_FILE)
    ] = None,
    reasoning_file: Annotated[
        Path | None, typer.Option(help="File with the agent's reasoning", **_IN_FILE)
    ] = None,
    spec: Annotated[Path | None, typer.Option(help="Task spec YAML", **_IN_FILE)] = None,
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
    llm_backend: _Llm = None,
    no_redact: Annotated[
        bool, typer.Option("--no-redact", help="Keep secrets and PII in evidence and reports")
    ] = False,
    run_id: Annotated[str | None, typer.Option(help="Override the run id")] = None,
    allow_mismatch: _AllowMismatch = False,
    screenshot_root: _ScreenshotRoot = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only set the exit code")] = False,
    debug: _Debug = False,
    otel: _Otel = False,
    langfuse_trace_id: _LangfuseTraceId = None,
) -> None:
    """Judge one agent run. Exit code: 0 pass, 1 fail, 2 needs review, 3 bad input, 4 internal."""
    _require_evidence(trace, har, browser_use)
    if answer is not None and answer_file is not None:
        raise InputUsageError("use either --answer or --answer-file, not both")

    err = _stderr()
    try:
        _check_exporters(otel, langfuse_trace_id)
        settings = _settings(llm_backend)
        task_spec = _load_spec(spec) if spec else None
        site_name = _site(site) if site is not None else None
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

        llm = None if offline else _build_llm(settings)
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
        raise _fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise _internal(e) from None

    if not quiet:
        print_verdict(verdict, Console(highlight=False))
        _print_written(err, (("json", out), ("html", html)))
        for warning in export_warnings:
            err.print(Text(warning, style="yellow"))
    raise typer.Exit(int(_OUTCOME_EXIT[verdict.outcome]))


@app.command()
def inspect(
    trace: _Trace = None,
    har: _Har = None,
    browser_use: _BrowserUse = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON")] = False,
    no_redact: Annotated[bool, typer.Option("--no-redact", help="Show raw URLs")] = False,
    debug: _Debug = False,
) -> None:
    """Summarise the evidence in a trace, HAR or browser-use run without judging it."""
    _require_evidence(trace, har, browser_use)
    try:
        settings = _settings()
        run = _load_run(
            trace=trace, har=har, browser_use=browser_use, max_body_chars=settings.max_body_chars
        )
        if settings.redact and not no_redact:
            run, _ = _redactor(settings, persistent=False).redact_run(run)
        summary = summarize_evidence(run)
    except CliError as e:
        raise _fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise _internal(e) from None
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
        try:
            _write_output(out, export_schema)
        except CliError as e:
            raise _fail(e) from None
        _stderr().print(Text.assemble(("wrote ", "dim"), str(out)))
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
        _stderr().print(
            Text.assemble(
                ("error: ", "bold red"),
                f"the MCP server needs the mcp extra: pip install 'evidence-judge[mcp]' ({e})",
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
    llm_backend: _Llm = None,
    no_redact: Annotated[
        bool, typer.Option("--no-redact", help="Judge unredacted evidence and agent text")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Do not print the report")] = False,
    debug: _Debug = False,
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
        settings = _settings(llm_backend)
        try:
            scenarios = load_scenarios(runs_dir, max_body_chars=settings.max_body_chars)
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
                _redactor(settings, persistent=llm is not None)
                if settings.redact and not no_redact
                else None
            ),
        )
        result = asyncio.run(runner.run(scenarios))
        if out:
            _write_output(out, lambda p: write_json(result, p))
        if markdown:
            _write_output(markdown, lambda p: write_markdown(result, p))
    except CliError as e:
        raise _fail(e) from None
    except Exception as e:
        if debug:
            raise
        raise _internal(e) from None

    if not quiet:
        typer.echo(to_markdown(result), nl=False)
        _print_written(_stderr(), (("json", out), ("markdown", markdown)))
