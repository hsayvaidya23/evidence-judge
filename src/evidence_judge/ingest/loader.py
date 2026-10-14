"""Load every recorded source of one run and merge them into a single Run."""

from __future__ import annotations

import re
from pathlib import Path

from evidence_judge.core.evidence import Channel, NetworkEvent, Run
from evidence_judge.ingest._json import as_float
from evidence_judge.ingest.browser_use import load_browser_use_history
from evidence_judge.ingest.errors import IngestError
from evidence_judge.ingest.har import load_har
from evidence_judge.ingest.playwright_trace import load_playwright_trace

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


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
) -> Run:
    """Load and merge a run's evidence.

    The trace is the primary source. A HAR recorded alongside it fills in bodies and statuses
    the trace lacks and adds requests the trace missed; the browser-use history contributes the
    agent's answer and reasoning. Explicit ``answer``/``answer_file``/``reasoning_file``/``task``
    override anything the sources carry. Per-source metadata is kept under
    ``metadata["sources"]``. Reasoning files hold one step per paragraph (blank-line separated).
    """
    if trace is None and har is None and browser_use is None:
        raise IngestError("load_run needs at least one of trace, har or browser_use")
    if answer is not None and answer_file is not None:
        raise IngestError("pass either answer or answer_file, not both")

    trace_run = load_playwright_trace(trace, max_body_chars=max_body_chars) if trace else None
    har_run = load_har(har, max_body_chars=max_body_chars) if har else None
    agent_run = load_browser_use_history(browser_use) if browser_use else None
    loaded = [r for r in (trace_run, har_run, agent_run) if r is not None]

    sources: list[Run] = []
    if trace_run is not None and har_run is not None:
        sources.append(attach_har(trace_run, har_run))
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


def attach_har(trace_run: Run, har_run: Run) -> Run:
    """Merge a HAR into a trace run's network evidence without duplicating requests.

    Requests are paired in order by method and URL. A paired trace request keeps its own
    timing and gains any body, status or headers only the HAR captured; unpaired HAR requests
    are shifted onto the trace clock and added. Network ids are reassigned chronologically.
    """
    offset = _clock_offset(trace_run, har_run)
    unmatched = list(har_run.network)
    network: list[NetworkEvent] = []
    for event in trace_run.network:
        partner = next(
            (h for h in unmatched if h.method == event.method and h.url == event.url), None
        )
        if partner is None:
            network.append(event)
            continue
        unmatched.remove(partner)
        network.append(_fill_gaps(event, partner))
    network.extend(h.model_copy(update={"ts": round(h.ts + offset, 3)}) for h in unmatched)
    network.sort(key=lambda n: n.ts)
    renumbered = [n.model_copy(update={"id": f"net#{i}"}) for i, n in enumerate(network, 1)]
    return trace_run.model_copy(
        update={"network": renumbered, "captured": trace_run.captured | {Channel.NETWORK}}
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
