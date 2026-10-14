from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, PackageLoader, StrictUndefined

from evidence_judge import __version__
from evidence_judge.core.claims import ClaimSet
from evidence_judge.core.evidence import ActionEvent, NetworkEvent, Run, Screenshot
from evidence_judge.core.verdict import (
    Citation,
    Dimension,
    Outcome,
    RunVerdict,
    Support,
)

MAX_TIMELINE_ROWS = 500
MAX_IMAGE_BYTES = 4 * 1024 * 1024
_IMAGE_MIME = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

_OUTCOME = {
    Outcome.PASS: ("Pass", "pass"),
    Outcome.FAIL: ("Fail", "fail"),
    Outcome.NEEDS_REVIEW: ("Needs review", "review"),
}
_SUPPORT = {
    Support.SUPPORTED: ("Supported", "ok"),
    Support.CONTRADICTED: ("Contradicted", "bad"),
    Support.NO_EVIDENCE: ("No evidence", "muted"),
}
_DIMENSION_LABEL = {
    Dimension.SUCCESS: "Success",
    Dimension.SIDE_EFFECTS: "Side effects",
    Dimension.STATE: "State",
    Dimension.GUARDRAILS: "Guardrails",
    Dimension.RECOVERY: "Recovery",
    Dimension.LOOPS: "Loops",
}

_env = Environment(
    loader=PackageLoader("evidence_judge.report", "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True, slots=True)
class CitationView:
    evidence_id: str
    excerpt: str | None
    href: str | None


@dataclass(frozen=True, slots=True)
class ClaimView:
    id: str
    text: str
    quote: str | None
    key: bool
    status_label: str
    tone: str
    reason: str
    checker: str
    confidence: str
    citations: list[CitationView]


@dataclass(frozen=True, slots=True)
class ExpectationView:
    id: str
    kind: str
    met_label: str
    tone: str
    reason: str
    citations: list[CitationView]


@dataclass(frozen=True, slots=True)
class DimensionView:
    label: str
    score: str
    tone: str
    findings: list[str]


@dataclass(frozen=True, slots=True)
class TimelineRow:
    anchor: str
    evidence_id: str
    time: str
    kind: str
    label: str
    detail: str | None
    status: str
    tone: str
    cited: bool


@dataclass(frozen=True, slots=True)
class ThumbView:
    anchor: str
    evidence_id: str
    caption: str
    data_uri: str


@dataclass(slots=True)
class _Ctx:
    cited: set[str] = field(default_factory=set[str])
    anchors: set[str] = field(default_factory=set[str])


def anchor_for(evidence_id: str) -> str:
    return "ev-" + re.sub(r"[^A-Za-z0-9_-]", "-", evidence_id)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{round(value * 100)}%"


def _seconds(ts: float) -> str:
    return f"{ts / 1000:.2f}s"


def _score_tone(score: float | None) -> str:
    if score is None:
        return "muted"
    if score >= 0.8:
        return "ok"
    return "warn" if score >= 0.5 else "bad"


def _short_url(url: str, limit: int = 96) -> tuple[str, str | None]:
    try:
        parts = urlsplit(url)
    except ValueError:
        return (url[:limit], None)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    if len(path) > limit:
        path = path[: limit - 1] + "…"
    return (path, parts.netloc or None)


def _http_tone(n: NetworkEvent) -> tuple[str, str]:
    if n.failed:
        return ("failed", "bad")
    if n.status is None:
        return ("—", "muted")
    return (str(n.status), "ok" if n.ok else "bad")


def _all_citations(verdict: RunVerdict) -> list[Citation]:
    out = [c for cv in verdict.claims for c in cv.citations]
    out.extend(c for ex in verdict.expectations for c in ex.citations)
    return out


def _action_row(a: ActionEvent, ctx: _Ctx) -> TimelineRow:
    target = a.selector or a.url or ""
    label = f"{a.name} {target}".strip()
    detail = a.error or (f"value: {a.value}" if a.value else None)
    status, tone = ("error", "bad") if a.error else ("ok", "ok")
    return TimelineRow(
        anchor=anchor_for(a.id),
        evidence_id=a.id,
        time=_seconds(a.ts),
        kind="action",
        label=label,
        detail=detail,
        status=status,
        tone=tone,
        cited=a.id in ctx.cited,
    )


def _request_row(n: NetworkEvent, ctx: _Ctx) -> TimelineRow:
    path, host = _short_url(n.url)
    status, tone = _http_tone(n)
    return TimelineRow(
        anchor=anchor_for(n.id),
        evidence_id=n.id,
        time=_seconds(n.ts),
        kind="request",
        label=f"{n.method.upper()} {path}",
        detail=host,
        status=status,
        tone=tone,
        cited=n.id in ctx.cited,
    )


def _timeline(run: Run, ctx: _Ctx) -> tuple[list[TimelineRow], int]:
    events: list[tuple[float, int, TimelineRow]] = [
        (a.ts, 0, _action_row(a, ctx)) for a in run.actions
    ]
    shown_requests = {n.id for n in run.network if n.is_mutation or n.id in ctx.cited}
    events.extend((n.ts, 1, _request_row(n, ctx)) for n in run.network if n.id in shown_requests)
    events.sort(key=lambda e: (e[0], e[1]))
    rows = [row for _, _, row in events]
    hidden = max(0, len(rows) - MAX_TIMELINE_ROWS)
    return rows[:MAX_TIMELINE_ROWS], hidden


def _data_uri(shot: Screenshot, run: Run) -> str | None:
    blob = run.blobs.get(shot.resource)
    mime = shot.mime_type.lower()
    if blob is None or mime not in _IMAGE_MIME or len(blob) > MAX_IMAGE_BYTES:
        return None
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def _final_shot(run: Run) -> Screenshot | None:
    if not run.screenshots:
        return None
    if run.final_state and run.final_state.screenshot_id:
        for s in run.screenshots:
            if s.id == run.final_state.screenshot_id:
                return s
    return run.screenshots[-1]


def _thumbnails(run: Run, ctx: _Ctx) -> list[ThumbView]:
    by_id = {s.id: s for s in run.screenshots}
    final = _final_shot(run)
    ordered: list[tuple[Screenshot, str]] = []
    seen: set[str] = set()
    for c in sorted(ctx.cited):
        shot = by_id.get(c)
        if shot is not None and shot.id not in seen:
            seen.add(shot.id)
            ordered.append((shot, f"{shot.id} · {_seconds(shot.ts)}"))
    ordered.sort(key=lambda p: p[0].ts)
    if final is not None:
        caption = f"Final · {final.id}"
        if final.id in seen:
            ordered = [(s, caption if s.id == final.id else c) for s, c in ordered]
        else:
            ordered.append((final, caption))
    thumbs: list[ThumbView] = []
    for shot, caption in ordered:
        uri = _data_uri(shot, run)
        if uri is not None:
            thumbs.append(
                ThumbView(
                    anchor=anchor_for(shot.id), evidence_id=shot.id, caption=caption, data_uri=uri
                )
            )
    return thumbs


def _citations(cites: list[Citation], ctx: _Ctx) -> list[CitationView]:
    out: list[CitationView] = []
    for c in cites:
        anchor = anchor_for(c.evidence_id)
        href = f"#{anchor}" if anchor in ctx.anchors else None
        out.append(CitationView(evidence_id=c.evidence_id, excerpt=c.excerpt, href=href))
    return out


def _context(verdict: RunVerdict, run: Run, claims: ClaimSet | None) -> dict[str, object]:
    ctx = _Ctx(cited={c.evidence_id for c in _all_citations(verdict)})
    rows, hidden = _timeline(run, ctx)
    thumbs = _thumbnails(run, ctx)
    ctx.anchors = {r.anchor for r in rows} | {t.anchor for t in thumbs}

    quotes = {c.id: c.quote for c in claims.claims} if claims else {}
    claim_views = [
        ClaimView(
            id=cv.claim_id,
            text=cv.claim_text,
            quote=quotes.get(cv.claim_id),
            key=cv.key,
            status_label=_SUPPORT[cv.status][0],
            tone=_SUPPORT[cv.status][1],
            reason=cv.reason,
            checker=cv.checker,
            confidence=_pct(cv.confidence),
            citations=_citations(cv.citations, ctx),
        )
        for cv in verdict.claims
    ]
    tally = {label: 0 for label, _ in _SUPPORT.values()}
    for cv in verdict.claims:
        tally[_SUPPORT[cv.status][0]] += 1

    expectations = [
        ExpectationView(
            id=ex.expectation_id,
            kind=ex.kind,
            met_label="Met" if ex.met else ("Not met" if ex.met is False else "Not assessable"),
            tone="ok" if ex.met else ("bad" if ex.met is False else "muted"),
            reason=ex.reason,
            citations=_citations(ex.citations, ctx),
        )
        for ex in verdict.expectations
    ]
    dimensions = [
        DimensionView(
            label=_DIMENSION_LABEL[d],
            score=_pct(verdict.dimensions[d].score),
            tone=_score_tone(verdict.dimensions[d].score),
            findings=verdict.dimensions[d].findings,
        )
        for d in Dimension
        if d in verdict.dimensions
    ]
    outcome_label, outcome_class = _OUTCOME[verdict.outcome]
    final = run.final_state
    return {
        "v": verdict,
        "outcome_label": outcome_label,
        "outcome_class": outcome_class,
        "score": round(verdict.score * 100),
        "claims": claim_views,
        "tally": [(label, n) for label, n in tally.items() if n],
        "expectations": expectations,
        "dimensions": dimensions,
        "timeline": rows,
        "timeline_hidden": hidden,
        "thumbs": thumbs,
        "final_url": final.url if final else None,
        "final_title": final.title if final else None,
        "source": run.source,
        "channels": sorted(c.value for c in run.captured),
        "coverage": _pct(verdict.claim_coverage),
        "deterministic": _pct(verdict.deterministic_share),
        "cost": f"${verdict.usage.cost_usd:.4f}",
        "latency": f"{verdict.usage.latency_ms / 1000:.1f}s",
        "tool_version": __version__,
    }


def render_html(verdict: RunVerdict, run: Run, *, claims: ClaimSet | None = None) -> str:
    """Render a self-contained HTML report. All agent-controlled text is HTML-escaped."""
    return _env.get_template("report.html.j2").render(_context(verdict, run, claims))


def write_html(
    verdict: RunVerdict, run: Run, path: Path, *, claims: ClaimSet | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(verdict, run, claims=claims), encoding="utf-8")
    return path
