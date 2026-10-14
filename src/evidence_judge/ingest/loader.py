"""Load every recorded source of one run and merge them into a single Run."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Final

from evidence_judge.core.evidence import Channel, NetworkEvent, Run
from evidence_judge.ingest._json import as_float
from evidence_judge.ingest.browser_use import load_browser_use_history
from evidence_judge.ingest.errors import IngestError, MismatchedEvidenceError
from evidence_judge.ingest.har import load_har
from evidence_judge.ingest.playwright_trace import load_playwright_trace

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
MIN_HAR_MATCH_RATIO: Final = 0.5
"""Share of trace requests a HAR must also contain to count as the same run."""
MIN_REQUESTS_TO_COMPARE: Final = 5
"""Traces with fewer requests than this are too small to judge a HAR mismatch."""


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
    if trace_run is not None and har_run is not None:
        sources.append(attach_har(trace_run, har_run, allow_mismatch=allow_mismatch))
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
            "metadata": {"sources": {r.source: r.metadata for r in loaded}},
        }
    )


def attach_har(trace_run: Run, har_run: Run, *, allow_mismatch: bool = False) -> Run:
    """Merge a HAR into a trace run's network evidence without duplicating requests.

    Requests are paired in order by method and URL. A paired trace request keeps its own
    timing and gains any body, status or headers only the HAR captured; unpaired HAR requests
    are shifted onto the trace clock and added. Network ids are reassigned chronologically.

    Raises:
        MismatchedEvidenceError: the trace has at least ``MIN_REQUESTS_TO_COMPARE`` requests,
            the HAR has entries, and fewer than ``MIN_HAR_MATCH_RATIO`` of the trace requests
            appear in the HAR, so the HAR is probably from another run. ``allow_mismatch=True``
            merges anyway.
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
    if not allow_mismatch:
        _check_same_run(len(trace_run.network), len(har_run.network), len(paired))
    network.extend(
        h.model_copy(update={"ts": round(h.ts + offset, 3)})
        for index, h in enumerate(har_run.network)
        if index not in paired
    )
    network.sort(key=lambda n: n.ts)
    renumbered = [n.model_copy(update={"id": f"net#{i}"}) for i, n in enumerate(network, 1)]
    return trace_run.model_copy(
        update={"network": renumbered, "captured": trace_run.captured | {Channel.NETWORK}}
    )


def _check_same_run(trace_requests: int, har_requests: int, paired: int) -> None:
    if trace_requests < MIN_REQUESTS_TO_COMPARE or har_requests == 0:
        return
    if paired >= MIN_HAR_MATCH_RATIO * trace_requests:
        return
    raise MismatchedEvidenceError(
        f"only {paired} of {trace_requests} trace requests appear in the HAR "
        f"({har_requests} entries); the HAR looks like a recording of another run. "
        "Pass allow_mismatch=True to merge it anyway."
    )


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
        }
    )


def _read_text(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return Path(path).read_text(encoding="utf-8").strip()


def _read_paragraphs(path: str | Path) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    return [p.strip() for p in _PARAGRAPH_BREAK.split(text) if p.strip()]
