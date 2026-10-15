"""What the MCP tools do, independent of the protocol. Every function takes paths the client
named, checks them against the allowed roots, and raises ``ToolInputError`` for anything the
caller can fix."""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from pydantic import ValidationError

from evidence_judge import engine, ingest
from evidence_judge.config import Settings, stable_redaction_key
from evidence_judge.core.evidence import Run
from evidence_judge.core.site import Site
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Dimension, RunVerdict, Support, TamperLevel
from evidence_judge.ingest import IngestError, MismatchedEvidenceError
from evidence_judge.llm import factory
from evidence_judge.llm.base import LLMClient, LLMConfigError, LLMError
from evidence_judge.mcp.models import (
    EvidenceSource,
    ExplainedItem,
    Explanation,
    FoundRecording,
    InspectResult,
    JudgeResult,
    Mutation,
    RecordingSearch,
    WrittenReport,
)
from evidence_judge.mcp.models import Injection as InjectionOut
from evidence_judge.mcp.paths import PathNotAllowedError, Roots, walk
from evidence_judge.mcp.specs import SpecCheck, describe, parse_spec
from evidence_judge.redact import RedactionReport, Redactor
from evidence_judge.report.console import summarize_evidence
from evidence_judge.report.html_report import write_html
from evidence_judge.report.json_report import write_json

MAX_VERDICT_BYTES = 50 * 1024 * 1024
MAX_ANSWER_CHARS = 200_000
SCAN_MAX_DEPTH = 6
SCAN_MAX_FILES = 20_000
SCAN_SKIP = (".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".tox")


class ToolInputError(Exception):
    """A request the caller can fix: a bad path, an invalid spec, unreadable evidence."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """What every tool call shares: the allowed roots and how settings are read."""

    roots: Roots
    settings: Callable[[], Settings] = Settings
    allow_llm: bool = True
    """False makes every judge_run offline whatever the client asks."""


@dataclass(slots=True)
class JudgeRequest:
    trace: str | None = None
    har: str | None = None
    browser_use: str | None = None
    answer: str | None = None
    answer_file: str | None = None
    reasoning_file: str | None = None
    task: str | None = None
    spec: str | None = None
    spec_yaml: str | None = None
    site: str | None = None
    offline: bool = True
    require_tamper_evidence: bool = False
    allow_mismatch: bool = False
    run_id: str | None = None
    json_report: str | None = None
    html_report: str | None = None
    overwrite: bool = False


@dataclass(frozen=True, slots=True)
class PreparedJudge:
    """A judge_run request with its paths checked, spec parsed and evidence loaded."""

    run: Run
    spec: TaskSpec | None
    settings: Settings
    json_path: Path | None
    html_path: Path | None


def settings_of(config: ServerConfig) -> Settings:
    try:
        return config.settings()
    except ValidationError as e:
        raise ToolInputError(f"invalid evidence-judge settings: {e}") from e


def _opt(roots: Roots, value: str | None, what: str, *, directory_ok: bool = False) -> Path | None:
    if value is None:
        return None
    try:
        return roots.existing(value, what=what) if directory_ok else roots.file(value, what=what)
    except PathNotAllowedError as e:
        raise ToolInputError(str(e)) from e


_HTML_MARKER: Final = b'<meta name="generator" content="evidence-judge '
_MARKER_BYTES: Final = 4096
_MAX_REPORT_BYTES: Final = 64 * 1024 * 1024


def is_report(path: Path, kind: Literal["json", "html"]) -> bool:
    """Whether ``path`` is a report evidence-judge wrote: a verdict JSON that validates, or an
    HTML page carrying the report's generator tag."""
    try:
        if kind == "html":
            with path.open("rb") as handle:
                return _HTML_MARKER in handle.read(_MARKER_BYTES)
        if path.stat().st_size > _MAX_REPORT_BYTES:
            return False
        RunVerdict.model_validate_json(path.read_bytes())
    except (OSError, ValueError):
        return False
    return True


def _out(
    roots: Roots, value: str | None, what: str, kind: Literal["json", "html"], overwrite: bool
) -> Path | None:
    """A report path inside the roots. An existing file is replaced only when it is an earlier
    evidence-judge report, or when ``overwrite`` is set."""
    if value is None:
        return None
    try:
        path = roots.output(value, what=what)
    except PathNotAllowedError as e:
        raise ToolInputError(str(e)) from e
    if path.exists() and not overwrite and not is_report(path, kind):
        raise ToolInputError(
            f"{what} {value!r} exists and is not an evidence-judge {kind} report; refusing to "
            "overwrite it (pass overwrite=true to replace it)"
        )
    return path


def _evidence_paths(
    roots: Roots, trace: str | None, har: str | None, browser_use: str | None
) -> tuple[Path | None, Path | None, Path | None]:
    if not (trace or har or browser_use):
        raise ToolInputError("provide evidence: trace, har and/or browser_use")
    return (
        _opt(roots, trace, "trace"),
        _opt(roots, har, "har"),
        _opt(roots, browser_use, "browser_use", directory_ok=True),
    )


def load_evidence(**kwargs: Any) -> Run:
    """``ingest.load_run`` with its input errors as ``ToolInputError``."""
    try:
        return ingest.load_run(**kwargs)
    except MismatchedEvidenceError as e:
        raise ToolInputError(
            f"could not load evidence: {e.reason} Set allow_mismatch to merge it anyway."
        ) from e
    except (IngestError, OSError, ValueError, zipfile.BadZipFile) as e:
        raise ToolInputError(f"could not load evidence: {e}") from e


def _load_spec(roots: Roots, path: str | None, text: str | None) -> TaskSpec | None:
    if path is not None and text is not None:
        raise ToolInputError("use either spec or spec_yaml, not both")
    if path is not None:
        spec_path = _opt(roots, path, "spec")
        assert spec_path is not None
        text = _read_text(spec_path, "spec")
    if text is None:
        return None
    spec, check = parse_spec(text)
    if spec is None:
        raise ToolInputError(f"invalid task spec:\n{describe(check)}")
    return spec


def _read_text(path: Path, what: str, *, max_bytes: int = 1024 * 1024) -> str:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise ToolInputError(f"{what} {path} is {size} bytes; the limit is {max_bytes}")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ToolInputError(f"cannot read {what} {path}: {e}") from e


def _site(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(Site.parse(value, what="site"))
    except ValueError as e:
        raise ToolInputError(str(e)) from e


def redactor_for(settings: Settings, *, persistent: bool) -> Redactor:
    """The run's redactor; only a ``persistent`` one (an LLM cache is used) reads or creates the
    key file, and an unwritable cache falls back to a per-process key."""
    if not persistent or settings.redaction_key is not None:
        return Redactor.from_settings(settings)
    try:
        key = stable_redaction_key(settings)
    except ValueError as e:
        raise ToolInputError(f"redaction key: {e}") from e
    except OSError:
        key = None
    return Redactor(key) if key is not None else Redactor.from_settings(settings)


def _describe_redaction(report: RedactionReport | None) -> str:
    if report is None:
        return "redaction disabled by EVJ_REDACT=false: the verdict may contain secrets"
    if not report.total:
        return "nothing sensitive found"
    parts = ", ".join(f"{k} {v}" for k, v in sorted(report.counts.items()))
    return f"redacted {report.total} values ({parts})"


def _llm(config: ServerConfig, settings: Settings, offline: bool) -> LLMClient | None:
    if offline:
        return None
    if not config.allow_llm:
        raise ToolInputError("this server was started with --offline-only; judge with offline=true")
    try:
        llm = factory.build_llm(settings)
    except LLMError as e:
        raise ToolInputError(f"could not initialise the LLM client: {e}") from e
    if llm is None:
        raise ToolInputError(
            "offline=false needs a configured model (set ANTHROPIC_API_KEY for the server), "
            "or judge with offline=true"
        )
    return llm


def prepare_judge(config: ServerConfig, req: JudgeRequest) -> PreparedJudge:
    """The blocking half of judge_run: checks paths, reads the spec and loads the evidence."""
    roots = config.roots
    if req.answer is not None and req.answer_file is not None:
        raise ToolInputError("use either answer or answer_file, not both")
    if req.answer is not None and len(req.answer) > MAX_ANSWER_CHARS:
        raise ToolInputError(f"answer exceeds {MAX_ANSWER_CHARS} characters; use answer_file")
    trace, har, browser_use = _evidence_paths(roots, req.trace, req.har, req.browser_use)
    answer_file = _opt(roots, req.answer_file, "answer_file")
    reasoning_file = _opt(roots, req.reasoning_file, "reasoning_file")
    json_path = _out(roots, req.json_report, "json_report", "json", req.overwrite)
    html_path = _out(roots, req.html_report, "html_report", "html", req.overwrite)
    settings = settings_of(config)
    spec = _load_spec(roots, req.spec, req.spec_yaml)
    run = load_evidence(
        trace=trace,
        har=har,
        browser_use=browser_use,
        answer=req.answer,
        answer_file=answer_file,
        reasoning_file=reasoning_file,
        task=req.task or (spec.task if spec else None),
        run_id=req.run_id,
        max_body_chars=settings.max_body_chars,
        allow_mismatch=req.allow_mismatch,
    )
    return PreparedJudge(run, spec, settings, json_path, html_path)


async def judge(config: ServerConfig, req: JudgeRequest, prepared: PreparedJudge) -> JudgeResult:
    """The async half of judge_run: redact, judge, and write the requested reports."""
    run, spec, settings = prepared.run, prepared.spec, prepared.settings
    site = _site(req.site)
    llm = _llm(config, settings, req.offline)
    report: RedactionReport | None = None
    if settings.redact:
        redactor = redactor_for(settings, persistent=llm is not None)
        run, report = redactor.redact_run(run)
        if spec is not None:
            spec, _ = redactor.redact_model(spec)
    options = engine.JudgeOptions(
        offline=llm is None,
        spec=spec,
        site=site,
        require_tamper_evidence=req.require_tamper_evidence,
    )
    try:
        verdict = await engine.Judge(settings, llm).judge(run, options)
    except LLMConfigError as e:
        raise ToolInputError(f"{e} (judge with offline=true to skip the model)") from e
    except LLMError as e:
        raise ToolInputError(f"LLM error: {e}") from e
    return JudgeResult(
        verdict=verdict,
        reports=_write_reports(verdict, run, prepared.json_path, prepared.html_path),
        redaction=_describe_redaction(report),
        used_llm=llm is not None,
    )


def _write_reports(
    verdict: RunVerdict, run: Run, json_path: Path | None, html_path: Path | None
) -> list[WrittenReport]:
    written: list[WrittenReport] = []
    try:
        if json_path is not None:
            write_json(verdict, json_path)
            written.append(WrittenReport(kind="json", path=str(json_path)))
        if html_path is not None:
            write_html(verdict, run, html_path)
            written.append(WrittenReport(kind="html", path=str(html_path)))
    except OSError as e:
        raise ToolInputError(f"cannot write report: {e.strerror or e}") from e
    return written


def inspect(
    config: ServerConfig, trace: str | None, har: str | None, browser_use: str | None
) -> InspectResult:
    roots = config.roots
    trace_path, har_path, bu_path = _evidence_paths(roots, trace, har, browser_use)
    settings = settings_of(config)
    run = load_evidence(
        trace=trace_path,
        har=har_path,
        browser_use=bu_path,
        max_body_chars=settings.max_body_chars,
    )
    redactor = Redactor.from_settings(settings) if settings.redact else None
    if redactor is not None:
        run, _ = redactor.redact_run(run)
    summary = summarize_evidence(run)
    sources, warnings = _sources(run)
    if redactor is not None:
        warnings = [redactor.redact_text(w)[0] for w in warnings]
    return InspectResult(
        run_id=summary.run_id,
        source=summary.source,
        sources=sources,
        captured=summary.captured,
        counts=summary.counts,
        mutations=[
            Mutation(id=m.id, method=m.method, status=m.status, url=m.url)
            for m in summary.mutations
        ],
        injections=[
            InjectionOut(id=i.id, call=i.call, sha256=i.sha256) for i in summary.injections
        ],
        final_url=summary.final_url,
        final_title=summary.final_title,
        warnings=warnings,
        redacted=redactor is not None,
    )


def _sources(run: Run) -> tuple[list[EvidenceSource], list[str]]:
    sources: list[EvidenceSource] = []
    warnings: list[str] = [str(w) for w in _as_list(run.metadata.get("merge_warnings"))]
    raw = run.metadata.get("sources")
    if isinstance(raw, dict):
        for name, value in cast(dict[str, Any], raw).items():
            details = dict(cast(dict[str, Any], value)) if isinstance(value, dict) else {}
            warnings.extend(f"{name}: {w}" for w in _as_list(details.pop("warnings", None)))
            sources.append(EvidenceSource(name=name, details=details))
    else:
        sources.append(EvidenceSource(name=run.source, details={}))
    har_only = _as_list(run.metadata.get("har_only_requests"))
    if har_only:
        warnings.append(
            f"{len(har_only)} request(s) appear only in the HAR: they can contradict claims but "
            "never prove them"
        )
    return sources, warnings


def _as_list(value: object) -> list[object]:
    return list(cast(list[object], value)) if isinstance(value, list) else []


def load_verdict(config: ServerConfig, path: str) -> RunVerdict:
    try:
        verdict_path = config.roots.file(path, what="verdict")
    except PathNotAllowedError as e:
        raise ToolInputError(str(e)) from e
    try:
        size = verdict_path.stat().st_size
        if size > MAX_VERDICT_BYTES:
            raise ToolInputError(
                f"verdict {path} is {size} bytes; the limit is {MAX_VERDICT_BYTES}"
            )
        data = verdict_path.read_bytes()
    except OSError as e:
        raise ToolInputError(f"cannot read verdict {path}: {e.strerror or e}") from e
    try:
        return RunVerdict.model_validate_json(data)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors(include_url=False, include_input=False, include_context=False)[:10]
        )
        raise ToolInputError(
            f"{path} is not an evidence-judge verdict JSON (write one with judge_run "
            f"json_report or `evidence-judge judge --out`): {problems}"
        ) from e


def explain(verdict: RunVerdict) -> Explanation:
    items: list[ExplainedItem] = []
    claims = sorted(
        (c for c in verdict.claims if c.status is not Support.SUPPORTED),
        key=lambda c: (not c.key, c.status is not Support.CONTRADICTED),
    )
    items.extend(
        ExplainedItem(
            kind="claim",
            id=c.claim_id,
            status=c.status.value,
            key=c.key,
            statement=f'{c.claim_text} ("{c.quote}")',
            why=c.reason,
            evidence=c.citations,
        )
        for c in claims
    )
    expectations = sorted(
        (e for e in verdict.expectations if e.met is not True), key=lambda e: e.met is None
    )
    items.extend(
        ExplainedItem(
            kind=_expectation_kind(e.kind),
            id=e.expectation_id,
            status="not_met" if e.met is False else "unknown",
            why=e.reason,
            evidence=e.citations,
        )
        for e in expectations
    )
    for d in Dimension:
        score = verdict.dimensions.get(d)
        if score is None or not score.findings or score.score == 1.0:
            continue
        items.append(
            ExplainedItem(
                kind="dimension",
                id=d.value,
                status="n/a" if score.score is None else f"{round(score.score * 100)}%",
                why="; ".join(score.findings),
            )
        )
    tamper = verdict.tamper_detection
    if tamper is not None and tamper.level is not TamperLevel.FULL:
        items.append(
            ExplainedItem(
                kind="tamper_detection",
                id="tamper_detection",
                status=tamper.level.value,
                why=tamper.reason,
            )
        )
    supported = sum(c.status is Support.SUPPORTED for c in verdict.claims)
    explanation = Explanation(
        run_id=verdict.run_id,
        outcome=verdict.outcome,
        score=verdict.score,
        summary=verdict.summary,
        supported_claims=supported,
        items=items,
        tamper_detection=tamper,
        warnings=verdict.warnings,
        text="",
    )
    return explanation.model_copy(update={"text": render_explanation(explanation)})


_ExpectationKind = Literal["expect", "forbid", "constraint"]
_EXPECTATION_KINDS: dict[str, _ExpectationKind] = {
    "expect": "expect",
    "forbid": "forbid",
    "constraint": "constraint",
}


def _expectation_kind(kind: str) -> _ExpectationKind:
    return _EXPECTATION_KINDS.get(kind, "expect")


def render_explanation(e: Explanation) -> str:
    lines = [
        f"**{e.outcome.value.upper()}** (score {round(e.score * 100)}/100), run `{e.run_id}`",
        "",
        e.summary,
    ]
    if e.supported_claims:
        lines += ["", f"{e.supported_claims} claim(s) were supported by the evidence."]
    if e.items:
        lines += ["", "Why it is not a clean pass:"]
        for item in e.items:
            label = f"{item.kind} `{item.id}`" + (" (key)" if item.key else "")
            lines.append(f"- {label}: **{item.status}**")
            if item.statement:
                lines.append(f"  - claim: {item.statement}")
            lines.append(f"  - why: {item.why}")
            if item.evidence:
                cited = ", ".join(
                    c.evidence_id + (f" ({c.excerpt})" if c.excerpt else "") for c in item.evidence
                )
                lines.append(f"  - evidence: {cited}")
    elif e.outcome.value == "pass":
        lines += ["", "Every claim and expectation was backed by the recorded evidence."]
    if e.warnings:
        lines += ["", "Warnings:", *(f"- {w}" for w in e.warnings)]
    return "\n".join(lines)


def validate_spec(config: ServerConfig, path: str | None, text: str | None) -> SpecCheck:
    if (path is None) == (text is None):
        raise ToolInputError("pass exactly one of path or spec_yaml")
    if path is not None:
        spec_path = _opt(config.roots, path, "spec")
        assert spec_path is not None
        text = _read_text(spec_path, "spec")
    assert text is not None
    return parse_spec(text)[1]


def find_recordings(config: ServerConfig, directory: str | None, limit: int) -> RecordingSearch:
    roots = config.roots
    try:
        top = roots.directory(directory, what="directory") if directory else roots.default
    except PathNotAllowedError as e:
        raise ToolInputError(str(e)) from e
    found: list[FoundRecording] = []
    scanned = 0
    truncated = False
    for here, files in walk(top, max_depth=SCAN_MAX_DEPTH, skip=SCAN_SKIP):
        scanned += len(files)
        if scanned > SCAN_MAX_FILES:
            truncated = True
            break
        found.extend(_recordings_in(here, files))
    found.sort(key=lambda r: r.modified, reverse=True)
    if len(found) > limit:
        truncated = True
    return RecordingSearch(directory=str(top), recordings=found[:limit], truncated=truncated)


def _recordings_in(here: Path, files: list[str]) -> list[FoundRecording]:
    out: list[FoundRecording] = []
    if any(f.endswith(".trace") for f in files) and any(f.endswith(".network") for f in files):
        out.append(
            _found(
                here,
                "playwright_trace_dir",
                note=(
                    "unzipped Playwright traces, as Playwright MCP's browser_stop_tracing "
                    "leaves them; to judge one, zip its trace-<id>.trace and "
                    "trace-<id>.network files with the resources directory"
                ),
            )
        )
    for name in sorted(files):
        path = here / name
        if path.is_symlink():
            continue
        lower = name.lower()
        if lower.endswith(".zip") and _is_trace_zip(path):
            out.append(_found(path, "playwright_trace"))
        elif lower.endswith(".har"):
            out.append(_found(path, "har"))
        elif lower.endswith(".json") and "history" in lower:
            out.append(_found(path, "browser_use"))
    return out


def _is_trace_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return any(n.endswith(".trace") for n in archive.namelist()[:10_000])
    except (OSError, zipfile.BadZipFile, ValueError):
        return False


_RecordingKind = Literal["playwright_trace", "playwright_trace_dir", "har", "browser_use"]


def _found(path: Path, kind: _RecordingKind, note: str | None = None) -> FoundRecording:
    stat = path.stat()
    size = stat.st_size
    if path.is_dir():
        size = sum(p.stat().st_size for p in path.iterdir() if p.is_file())
    modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(timespec="seconds")
    return FoundRecording(path=str(path), kind=kind, size_bytes=size, modified=modified, note=note)


def summarize_verdict(verdict: RunVerdict) -> str:
    """A few lines for a model to read: the outcome and what decided it."""
    counts = {s: sum(c.status is s for c in verdict.claims) for s in Support}
    lines = [
        f"{verdict.outcome.value.upper()} score {round(verdict.score * 100)}/100: {verdict.summary}",
        (
            f"claims: {counts[Support.SUPPORTED]} supported, "
            f"{counts[Support.CONTRADICTED]} contradicted, {counts[Support.NO_EVIDENCE]} "
            "without evidence"
        ),
    ]
    unmet = [e.expectation_id for e in verdict.expectations if e.met is not True]
    if unmet:
        lines.append(f"spec entries not met or unknown: {', '.join(unmet)}")
    if verdict.tamper_detection is not None:
        lines.append(f"tamper detection: {verdict.tamper_detection.level.value}")
    return "\n".join(lines)
