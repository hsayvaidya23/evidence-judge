from __future__ import annotations

import base64
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, PackageLoader, StrictUndefined

from evidence_judge._version import __version__
from evidence_judge.core.evidence import ActionEvent, NetworkEvent, Run, Screenshot
from evidence_judge.core.verdict import (
    Citation,
    ClaimVerdict,
    Dimension,
    ExpectationResult,
    Outcome,
    RunVerdict,
    Support,
)

MAX_TIMELINE_ROWS = 500
MAX_IMAGE_BYTES = 4 * 1024 * 1024
REASON_PREVIEW_PARTS = 1
_ANCHOR_UNSAFE = re.compile(r"[^A-Za-z0-9]")
_IMAGE_MIME = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

_OUTCOME = {
    Outcome.PASS: ("Pass", "pass"),
    Outcome.FAIL: ("Fail", "fail"),
    Outcome.NEEDS_REVIEW: ("Needs review", "review"),
}
_GLYPH = {"ok": "✓", "bad": "✗", "muted": "?"}
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
    detail: str | None
    tone: str


@dataclass(frozen=True, slots=True)
class ClaimView:
    id: str
    text: str
    quote: str
    key: bool
    status_label: str
    tone: str
    reason: str
    reason_head: str
    reason_rest: list[str]
    checker: str
    confidence: str
    citations: list[CitationView]


@dataclass(frozen=True, slots=True)
class ExpectationView:
    id: str
    kind: str
    met_label: str
    tone: str
    glyph: str
    reason: str
    citations: list[CitationView]


@dataclass(frozen=True, slots=True)
class DimensionView:
    label: str
    score: str
    tone: str
    bar: int
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
    mutation: bool
    offset: float


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
    span: float = 0.0
    described: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])


def anchor_for(evidence_id: str) -> str:
    """A collision-free HTML id: letters and digits stay, any other character becomes
    ``-<hex code point>-`` (``net#1`` -> ``ev-net-23-1``, ``net-1`` -> ``ev-net-2d-1``)."""
    return "ev-" + _ANCHOR_UNSAFE.sub(lambda m: f"-{ord(m.group(0)):x}-", evidence_id)


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


def _offset(ts: float, ctx: _Ctx) -> float:
    return round(min(100.0, max(0.0, ts / ctx.span * 100)), 2) if ctx.span > 0 else 0.0


def _split_reason(reason: str) -> tuple[str, list[str]]:
    parts = [p.strip() for p in reason.split("; ") if p.strip()]
    if len(parts) <= REASON_PREVIEW_PARTS:
        return (reason, [])
    return ("; ".join(parts[:REASON_PREVIEW_PARTS]), parts[REASON_PREVIEW_PARTS:])


def _describe_evidence(run: Run) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for n in run.network:
        path, _ = _short_url(n.url, limit=48)
        status, tone = _http_tone(n)
        out[n.id] = (f"{n.method.upper()} {path} · {status}", tone)
    for a in run.actions:
        target = a.selector or (_short_url(a.url, limit=40)[0] if a.url else "")
        out[a.id] = (f"{a.name} {target}".strip(), "bad" if a.error else "ok")
    for s in run.snapshots:
        out[s.id] = (f"page {_short_url(s.url, limit=40)[0]}", "muted")
    for shot in run.screenshots:
        out[shot.id] = (f"screenshot · {_seconds(shot.ts)}", "muted")
    for c in run.console:
        out[c.id] = (f"console.{c.level}", "bad" if c.level == "error" else "muted")
    if run.final_state is not None:
        out["final"] = (f"final page {_short_url(run.final_state.url, limit=40)[0]}", "muted")
    return out


def _headline(verdict: RunVerdict) -> str:
    total = len(verdict.claims)
    bad = sum(1 for c in verdict.claims if c.status is Support.CONTRADICTED)
    missing = sum(1 for c in verdict.claims if c.status is Support.NO_EVIDENCE)
    unmet = sum(1 for e in verdict.expectations if e.met is False)
    if total == 0:
        return "The agent made no checkable claims"
    noun = "claim" if total == 1 else "claims"
    if bad:
        verb = "is" if bad == 1 else "are"
        return f"{bad} of {total} {noun} {verb} contradicted by the recording"
    if missing:
        verb = "has" if missing == 1 else "have"
        return f"{missing} of {total} {noun} {verb} no evidence in the recording"
    if unmet:
        return f"Every claim checks out, but {unmet} task expectation{'s' if unmet != 1 else ''} failed"
    return f"All {total} {noun} backed by recorded evidence"


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
        mutation=False,
        offset=_offset(a.ts, ctx),
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
        mutation=n.is_mutation,
        offset=_offset(n.ts, ctx),
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
        detail, tone = ctx.described.get(c.evidence_id, (None, "muted"))
        out.append(
            CitationView(
                evidence_id=c.evidence_id, excerpt=c.excerpt, href=href, detail=detail, tone=tone
            )
        )
    return out


def _claim_view(cv: ClaimVerdict, ctx: _Ctx) -> ClaimView:
    head, rest = _split_reason(cv.reason)
    return ClaimView(
        id=cv.claim_id,
        text=cv.claim_text,
        quote=cv.quote,
        key=cv.key,
        status_label=_SUPPORT[cv.status][0],
        tone=_SUPPORT[cv.status][1],
        reason=cv.reason,
        reason_head=head,
        reason_rest=rest,
        checker=cv.checker,
        confidence=_pct(cv.confidence),
        citations=_citations(cv.citations, ctx),
    )


def _met(met: bool | None) -> tuple[str, str]:
    if met:
        return "Met", "ok"
    if met is False:
        return "Not met", "bad"
    return "Not assessable", "muted"


def _expectation_view(ex: ExpectationResult, ctx: _Ctx) -> ExpectationView:
    label, tone = _met(ex.met)
    return ExpectationView(
        id=ex.expectation_id,
        kind=ex.kind,
        met_label=label,
        tone=tone,
        glyph=_GLYPH[tone],
        reason=ex.reason,
        citations=_citations(ex.citations, ctx),
    )


def _dimension_views(verdict: RunVerdict) -> list[DimensionView]:
    return [
        DimensionView(
            label=_DIMENSION_LABEL[d],
            score=_pct(verdict.dimensions[d].score),
            tone=_score_tone(verdict.dimensions[d].score),
            bar=round((verdict.dimensions[d].score or 0.0) * 100),
            findings=verdict.dimensions[d].findings,
        )
        for d in Dimension
        if d in verdict.dimensions
    ]


@dataclass(frozen=True, slots=True)
class EvidenceView:
    """What the report shows of the recording beside the verdict: its source, the timeline that
    citations link to, and how each cited evidence id is described."""

    source: str
    channels: list[str]
    timeline: list[TimelineRow]
    timeline_hidden: int
    described: dict[str, tuple[str, str]]
    timeline_note: str
    timeline_empty: str
    thumbs: list[ThumbView] = field(default_factory=list[ThumbView])
    final_url: str | None = None
    final_title: str | None = None


def cited_ids(verdict: RunVerdict) -> set[str]:
    return {c.evidence_id for c in _all_citations(verdict)}


def _browser_evidence(verdict: RunVerdict, run: Run) -> EvidenceView:
    stamps = [a.ts for a in run.actions] + [n.ts for n in run.network]
    ctx = _Ctx(cited=cited_ids(verdict), span=max(stamps, default=0.0))
    rows, hidden = _timeline(run, ctx)
    final = run.final_state
    return EvidenceView(
        source=run.source,
        channels=sorted(c.value for c in run.captured),
        timeline=rows,
        timeline_hidden=hidden,
        described=_describe_evidence(run),
        timeline_note="actions and state-changing requests",
        timeline_empty="No actions or state-changing requests were captured.",
        thumbs=_thumbnails(run, ctx),
        final_url=final.url if final else None,
        final_title=final.title if final else None,
    )


def _tally(verdict: RunVerdict) -> list[tuple[str, int, str]]:
    counts = Counter(cv.status for cv in verdict.claims)
    return [(label, counts[s], tone) for s, (label, tone) in _SUPPORT.items() if counts[s]]


def _verification(verdict: RunVerdict) -> str | None:
    if verdict.verification is None:
        return None
    return f"{verdict.verification.method.value}: {verdict.verification.reason}"


def _context(verdict: RunVerdict, evidence: EvidenceView) -> dict[str, object]:
    ctx = _Ctx(
        anchors={r.anchor for r in evidence.timeline} | {t.anchor for t in evidence.thumbs},
        described=evidence.described,
    )
    claim_views = [_claim_view(cv, ctx) for cv in verdict.claims]
    expectations = [_expectation_view(ex, ctx) for ex in verdict.expectations]
    dimensions = _dimension_views(verdict)
    outcome_label, outcome_class = _OUTCOME[verdict.outcome]
    return {
        "v": verdict,
        "outcome_label": outcome_label,
        "outcome_class": outcome_class,
        "score": round(verdict.score * 100),
        "claims": claim_views,
        "tally": _tally(verdict),
        "headline": _headline(verdict),
        "expectations": expectations,
        "dimensions": dimensions,
        "timeline": evidence.timeline,
        "timeline_hidden": evidence.timeline_hidden,
        "timeline_note": evidence.timeline_note,
        "timeline_empty": evidence.timeline_empty,
        "thumbs": evidence.thumbs,
        "final_url": evidence.final_url,
        "final_title": evidence.final_title,
        "source": evidence.source,
        "channels": evidence.channels,
        "verification": _verification(verdict),
        "coverage": _pct(verdict.claim_coverage),
        "deterministic": _pct(verdict.deterministic_share),
        "cost": f"${verdict.usage.cost_usd:.4f}",
        "latency": f"{verdict.usage.latency_ms / 1000:.1f}s",
        "tool_version": __version__,
    }


def render_report(verdict: RunVerdict, evidence: EvidenceView) -> str:
    """Render a self-contained HTML report of `verdict` beside `evidence`. All agent-controlled
    text is HTML-escaped."""
    return _env.get_template("report.html.j2").render(_context(verdict, evidence))


def render_html(verdict: RunVerdict, run: Run) -> str:
    """Render a self-contained HTML report. All agent-controlled text is HTML-escaped."""
    return render_report(verdict, _browser_evidence(verdict, run))


def write_html(verdict: RunVerdict, run: Run, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(verdict, run), encoding="utf-8")
    return path
