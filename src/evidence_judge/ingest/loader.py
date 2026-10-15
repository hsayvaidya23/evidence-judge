"""Load every recorded source of one run and merge them into a single Run."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Final

from evidence_judge.core.evidence import MUTATION_METHODS, Channel, NetworkEvent, Run
from evidence_judge.ingest._json import as_float
from evidence_judge.ingest.browser_use import load_browser_use_history
from evidence_judge.ingest.errors import IngestError, MismatchedEvidenceError
from evidence_judge.ingest.har import load_har
from evidence_judge.ingest.playwright_trace import load_playwright_trace

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
MIN_HAR_MATCH_RATIO: Final = 0.5
"""Share of trace requests a HAR must also contain to count as the same run, and share of HAR
entries that must pair with a trace request or (when both clocks are known) start inside the
trace's time window."""
MIN_REQUESTS_TO_COMPARE: Final = 5
"""Traces with at least this many requests must share ``MIN_HAR_MATCH_RATIO`` of them with a HAR;
smaller traces with any request must share at least one."""
HAR_WINDOW_SLACK_MS: Final = 2_000
"""How far outside the trace's time window a HAR entry may start and still count as in it."""
MAX_HAR_CLOCK_GAP_MS: Final = 10 * 60 * 1000
"""HAR-only requests starting further than this outside the trace's time window are dropped."""
"""HAR-only requests with these methods are dropped unless ``allow_mismatch``: a side effect the
trace never saw cannot be evidence on the HAR's word alone."""


def load_run(
    *,
    trace: str | Path | None = None,
    har: str | Path | None = None,
    browser_use: str | Path | None = None,
    answer: str | None = None,
    answer_file: str | Path | None = None,
    reasoning_file: str | Path | None = None,
    task: str | None = None,
    run_id: str | None = None,
    max_body_chars: int = 20_000,
    allow_mismatch: bool = False,
    screenshot_root: str | Path | None = None,
) -> Run:
    """Load and merge a run's evidence.

    The trace is the primary source. A HAR recorded alongside it fills in bodies and statuses
    the trace lacks and adds requests the trace missed; the browser-use history contributes the
    agent's answer and reasoning. Explicit ``answer``/``answer_file``/``reasoning_file``/``task``
    override anything the sources carry. Per-source metadata is kept under
    ``metadata["sources"]``. Reasoning files hold one step per paragraph (blank-line separated).

    Raises:
        MismatchedEvidenceError: the HAR does not look like the trace's run (see
            ``attach_har``); pass ``allow_mismatch=True`` to merge anyway.
    """
    if trace is None and har is None and browser_use is None:
        raise IngestError("load_run needs at least one of trace, har or browser_use")
    if answer is not None and answer_file is not None:
        raise IngestError("pass either answer or answer_file, not both")

    trace_run = load_playwright_trace(trace, max_body_chars=max_body_chars) if trace else None
    har_run = load_har(har, max_body_chars=max_body_chars) if har else None
    agent_run = (
        load_browser_use_history(browser_use, screenshot_root=screenshot_root)
        if browser_use
        else None
    )
    loaded = [r for r in (trace_run, har_run, agent_run) if r is not None]

    sources: list[Run] = []
    merge_metadata: dict[str, object] = {}
    if trace_run is not None and har_run is not None:
        attached = attach_har(trace_run, har_run, allow_mismatch=allow_mismatch)
        merge_metadata = {
            k: v
            for k, v in attached.metadata.items()
            if k in ("har_only_requests", "merge_warnings")
        }
        sources.append(attached)
    else:
        sources.extend(r for r in (trace_run, har_run) if r is not None)
    if agent_run is not None:
        sources.append(agent_run)

    merged = sources[0]
    for other in sources[1:]:
        merged = merged.merge(other)

    explicit_answer = answer if answer is not None else _read_text(answer_file)
    reasoning = _read_paragraphs(reasoning_file) if reasoning_file else merged.agent_reasoning
    return merged.model_copy(
        update={
            "run_id": run_id or merged.run_id,
            "source": "+".join(r.source for r in loaded),
            "task": task or merged.task,
            "agent_answer": explicit_answer if explicit_answer is not None else merged.agent_answer,
            "agent_reasoning": reasoning,
            "metadata": {"sources": {r.source: r.metadata for r in loaded}, **merge_metadata},
        }
    )


def attach_har(trace_run: Run, har_run: Run, *, allow_mismatch: bool = False) -> Run:
    """Merge a HAR into a trace run's network evidence without duplicating requests.

    Requests are paired in order by method and URL. A paired trace request keeps its own
    timing and gains any body, status or headers only the HAR captured; unpaired HAR requests
    are shifted onto the trace clock and added. Network ids are reassigned chronologically.

    The trace did not see the unpaired HAR requests, so their new ids are listed in
    ``metadata["har_only_requests"]``. Unless ``allow_mismatch``, unpaired HAR requests that
    mutate (``MUTATION_METHODS``) are dropped, and when both clocks are known, unpaired HAR
    requests starting more than ``MAX_HAR_CLOCK_GAP_MS`` outside the trace's time window are
    dropped; each drop is noted in ``metadata["merge_warnings"]``.

    Raises:
        MismatchedEvidenceError: the HAR does not look like a recording of the trace's run: it
            shares no request with a trace of fewer than ``MIN_REQUESTS_TO_COMPARE`` requests,
            or fewer than ``MIN_HAR_MATCH_RATIO`` of a larger trace's; fewer than
            ``MIN_HAR_MATCH_RATIO`` of its entries pair with the trace or (both clocks known)
            start within ``HAR_WINDOW_SLACK_MS`` of the trace's time window; or none of its
            entries do. ``allow_mismatch=True`` merges anyway.
    """
    offset = _clock_offset(trace_run, har_run)
    pending: defaultdict[tuple[str, str], deque[int]] = defaultdict(deque)
    for index, har_event in enumerate(har_run.network):
        pending[(har_event.method, har_event.url)].append(index)
    paired: set[int] = set()
    network: list[NetworkEvent] = []
    for event in trace_run.network:
        queue = pending.get((event.method, event.url))
        if not queue:
            network.append(event)
            continue
        index = queue.popleft()
        paired.add(index)
        network.append(_fill_gaps(event, har_run.network[index]))
    timed = _clocks_known(trace_run, har_run)
    window = _trace_window(trace_run) if timed else None
    shifted = [round(h.ts + offset, 3) for h in har_run.network]
    if not allow_mismatch:
        _check_same_run(len(trace_run.network), len(har_run.network), len(paired))
        if window is not None:
            _check_time_window(shifted, paired, window)
        elif trace_run.network:
            _check_har_share(len(har_run.network), len(paired), len(paired), timed=False)
    har_only: list[NetworkEvent] = []
    late = 0
    mutations = 0
    for index, h in enumerate(har_run.network):
        if index in paired:
            continue
        ts = shifted[index]
        if allow_mismatch:
            har_only.append(h.model_copy(update={"ts": ts}))
        elif h.method.upper() in MUTATION_METHODS:
            mutations += 1
        elif window is not None and not _near(ts, window, MAX_HAR_CLOCK_GAP_MS):
            late += 1
        else:
            har_only.append(h.model_copy(update={"ts": ts}))
    marker = {id(h) for h in har_only}
    network.extend(har_only)
    network.sort(key=lambda n: n.ts)
    renumbered = [n.model_copy(update={"id": f"net#{i}"}) for i, n in enumerate(network, 1)]
    metadata = {
        **trace_run.metadata,
        "har_only_requests": [
            r.id for r, n in zip(renumbered, network, strict=True) if id(n) in marker
        ],
    }
    warnings: list[str] = []
    if mutations:
        warnings.append(
            f"dropped {mutations} HAR-only POST/PUT/PATCH/DELETE requests the trace did not "
            "record; pass allow_mismatch=True to keep them"
        )
    if late:
        warnings.append(
            f"dropped {late} HAR requests that started more than "
            f"{MAX_HAR_CLOCK_GAP_MS // 60_000} minutes outside the trace"
        )
    if warnings:
        metadata["merge_warnings"] = warnings
    return trace_run.model_copy(
        update={
            "network": renumbered,
            "captured": trace_run.captured | {Channel.NETWORK},
            "metadata": metadata,
        }
    )


def _check_same_run(trace_requests: int, har_requests: int, paired: int) -> None:
    if trace_requests == 0 or har_requests == 0:
        return
    if trace_requests < MIN_REQUESTS_TO_COMPARE:
        if paired:
            return
        raise MismatchedEvidenceError(
            f"the HAR ({har_requests} entries) shares none of the {trace_requests} trace "
            "requests; it looks like a recording of another run."
        )
    if paired >= MIN_HAR_MATCH_RATIO * trace_requests:
        return
    raise MismatchedEvidenceError(
        f"only {paired} of {trace_requests} trace requests appear in the HAR "
        f"({har_requests} entries); the HAR looks like a recording of another run."
    )


def _check_time_window(shifted: list[float], paired: set[int], window: tuple[float, float]) -> None:
    if not shifted:
        return
    inside = {i for i, ts in enumerate(shifted) if _near(ts, window, HAR_WINDOW_SLACK_MS)}
    if not inside:
        raise MismatchedEvidenceError(
            f"the HAR's time window ({min(shifted) / 1000:+.1f}s to {max(shifted) / 1000:+.1f}s "
            "from the trace start) does not overlap the trace's "
            f"({window[0] / 1000:+.1f}s to {window[1] / 1000:+.1f}s); it looks like a recording "
            "of another run."
        )
    _check_har_share(len(shifted), len(paired | inside), len(inside), timed=True)


def _check_har_share(har_requests: int, accounted: int, inside: int, *, timed: bool) -> None:
    if har_requests == 0 or accounted >= MIN_HAR_MATCH_RATIO * har_requests:
        return
    reason = (
        f"only {inside} of {har_requests} HAR entries started inside the trace's time window"
        if timed
        else f"only {accounted} of {har_requests} HAR entries pair with a trace request"
    )
    raise MismatchedEvidenceError(f"{reason}; the HAR looks like a recording of another run.")


def _near(ts: float, window: tuple[float, float], slack: float) -> bool:
    return window[0] - slack <= ts <= window[1] + slack


def _clocks_known(trace_run: Run, har_run: Run) -> bool:
    return all(
        as_float(r.metadata.get("start_wall_time_ms")) is not None for r in (trace_run, har_run)
    )


def _trace_window(run: Run) -> tuple[float, float] | None:
    times = [
        *(n.ts for n in run.network),
        *(a.ts for a in run.actions),
        *(s.ts for s in run.snapshots),
        *(s.ts for s in run.screenshots),
        *(c.ts for c in run.console),
    ]
    return (min(0.0, *times), max(times)) if times else None


def _clock_offset(trace_run: Run, har_run: Run) -> float:
    trace_start = as_float(trace_run.metadata.get("start_wall_time_ms"))
    har_start = as_float(har_run.metadata.get("start_wall_time_ms"))
    if trace_start is None or har_start is None:
        return 0.0
    return har_start - trace_start


def _fill_gaps(event: NetworkEvent, har_event: NetworkEvent) -> NetworkEvent:
    return event.model_copy(
        update={
            "status": event.status if event.status is not None else har_event.status,
            "resource_type": event.resource_type or har_event.resource_type,
            "request_headers": event.request_headers or har_event.request_headers,
            "request_body": event.request_body or har_event.request_body,
            "response_headers": event.response_headers or har_event.response_headers,
            "response_body": event.response_body or har_event.response_body,
            "mime_type": event.mime_type or har_event.mime_type,
            "failed": event.failed and har_event.failed,
            "synthetic": event.synthetic or har_event.synthetic,
        }
    )


def _read_text(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return Path(path).read_text(encoding="utf-8").strip()


def _read_paragraphs(path: str | Path) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    return [p.strip() for p in _PARAGRAPH_BREAK.split(text) if p.strip()]
