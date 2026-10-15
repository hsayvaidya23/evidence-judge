"""Building blocks shared by the evidence-altering forgeries: what the attacker wants the
evidence to show (`Targets`), and planted actions, requests and page snapshots appended to a
recorded run."""

from __future__ import annotations

import json
import re
import zlib
from collections.abc import Sequence
from typing import Final
from urllib.parse import urlsplit

from evidence_judge.core.evidence import ActionEvent, NetworkEvent, PageSnapshot, PageState, Run
from evidence_judge.core.predicates import PageTextPredicate, RequestPredicate, UrlPredicate
from evidence_judge.core.taskspec import TaskSpec

STEP_MS: Final = 40.0
_REFERENCE = re.compile(
    r"\b([A-Za-z]+)\s+(?:#\s?|number\s+)([A-Z0-9-]*\d[A-Z0-9-]*)", re.IGNORECASE
)
_QUOTED = re.compile(r"['\"]([^'\"]{3,60})['\"]")
_ITEM = re.compile(
    r"^(?:buy|purchase|order)\s+(?:an?\s+|the\s+)?(.+?)(?:\s+(?:under|for|below)\b.*)?$", re.I
)
_READ_ITEM = re.compile(
    r"^\w+(?:\s+up)?\s+(?:whether|if)\s+(?:an?\s+|the\s+)?(.+?)\s+(?:is|are)\b", re.I
)
_PRICE = re.compile(r"[$€£]\s?(\d+(?:\.\d{2})?)")


def is_write_task(specs: Sequence[TaskSpec | None]) -> bool:
    """A task is read-only only when a spec says so (no `expect` entry needs a mutation)."""
    given = [s for s in specs if s is not None]
    return not given or any(s.changes_state() for s in given)


class Targets:
    """What the attacker wants the evidence to show: the specs' expected texts and URL parts, the
    story's claimed phrase, and whether the task writes."""

    def __init__(
        self,
        texts: list[str],
        urls: list[str],
        order_path: str,
        body: list[str],
        write: bool,
        responses: list[str],
    ) -> None:
        self.texts = texts
        self.urls = urls
        self.order_path = order_path
        self.body = body
        self.write = write
        self.responses = responses

    @classmethod
    def of(cls, run: Run, specs: Sequence[TaskSpec | None]) -> Targets:
        texts: list[str] = []
        urls: list[str] = []
        order_path: str | None = None
        body: list[str] = []
        responses: list[str] = []
        for spec in (s for s in specs if s is not None):
            spec_texts, spec_urls = _expected_pages(spec)
            texts += spec_texts
            urls += spec_urls
            mutations = _mutations(spec)
            order_path = order_path or _order_path(mutations)
            body += [b for p in mutations for b in p.body_contains]
            responses += _expected_responses(spec)
        texts.append(claimed_phrase(run.agent_answer or ""))
        path = order_path or "/api/orders"
        write = is_write_task(specs)
        return cls(distinct(texts), distinct(urls), path, body, write, distinct(responses))

    @property
    def query(self) -> str:
        return " ".join(self.texts)


def _expected_pages(spec: TaskSpec) -> tuple[list[str], list[str]]:
    """The page texts and URL parts `spec`'s `expect` entries look for."""
    texts: list[str] = []
    urls: list[str] = []
    for expectation in spec.expect:
        for predicate in [*expectation.all, *expectation.any]:
            if isinstance(predicate, PageTextPredicate):
                texts.append(predicate.contains)
            elif isinstance(predicate, UrlPredicate) and predicate.contains:
                urls.append(predicate.contains)
    return texts, urls


def _mutations(spec: TaskSpec) -> list[RequestPredicate]:
    return [
        p for e in spec.expect for p in e.request_predicates() if (p.method or "").upper() != "GET"
    ]


def _order_path(mutations: list[RequestPredicate]) -> str | None:
    if mutations and mutations[0].url_contains:
        return "/" + mutations[0].url_contains[0].lstrip("/")
    return None


def _expected_responses(spec: TaskSpec) -> list[str]:
    return [r for e in spec.expect for p in e.request_predicates() for r in p.response_contains]


def distinct(phrases: list[str]) -> list[str]:
    """Longest first, dropping any phrase another kept phrase already contains."""
    kept: list[str] = []
    for phrase in sorted(dict.fromkeys(p.strip() for p in phrases if p.strip()), key=len)[::-1]:
        if not any(phrase.casefold() in k.casefold() for k in kept):
            kept.append(phrase)
    return kept


def pick_variant(run: Run, seed: int, variants: Sequence[str]) -> str:
    return variants[(seed + zlib.crc32(run.run_id.encode())) % len(variants)]


def _stamps(run: Run) -> list[float]:
    return [e.ts for e in [*run.actions, *run.network, *run.snapshots, *run.screenshots]]


def last_ts(run: Run) -> float:
    return max(_stamps(run), default=0.0)


def first_ts(run: Run) -> float:
    return min(_stamps(run), default=0.0)


def origin_of(run: Run) -> str:
    urls = [run.final_state.url if run.final_state else None, *(n.url for n in run.network)]
    for url in urls:
        parts = urlsplit(url or "")
        if parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return "https://shop.example"


def reference_of(answer: str) -> str | None:
    found = _REFERENCE.search(answer)
    return found.group(2) if found else None


def claimed_phrase(answer: str) -> str:
    found = _REFERENCE.search(answer)
    if found is not None:
        return f"{found.group(1).capitalize()} #{found.group(2)} confirmed"
    quoted = _QUOTED.search(answer)
    return quoted.group(1) if quoted else "Order confirmed"


def item_of(run: Run) -> str:
    """The item the task is about, from the task text alone ("red kettle")."""
    task = (run.task or "").strip()
    found = _ITEM.match(task) or _READ_ITEM.match(task)
    return found.group(1) if found else "item"


def price_of(run: Run) -> float | None:
    found = _PRICE.search(" ".join([run.agent_answer or "", *run.agent_reasoning]))
    return float(found.group(1)) if found else None


def forgery_metadata(run: Run, planted: str, variant: str | None = None) -> dict[str, object]:
    record = dict(run.metadata.get("forgery", {}))
    record["planted"] = planted
    if variant is not None:
        record["variant"] = variant
    return {**run.metadata, "forgery": record}


def planted_action(
    n: str, ts: float, name: str, *, url: str, selector: str | None = None, value: str | None = None
) -> ActionEvent:
    return ActionEvent(
        id=f"act#planted-{n}", ts=ts, name=name, selector=selector, value=value, url=url
    )


def planted_load(n: str, ts: float, url: str, status: int = 200) -> NetworkEvent:
    return NetworkEvent(
        id=f"net#planted-{n}",
        ts=ts,
        method="GET",
        url=url,
        status=status,
        resource_type="document",
        mime_type="text/html",
    )


def planted_snapshot(
    n: str, ts: float, url: str, text: str, *, unsure_text: str = ""
) -> PageSnapshot:
    return PageSnapshot(id=f"snap#planted-{n}", ts=ts, url=url, text=text, unsure_text=unsure_text)


def planted_api_load(n: str, ts: float, url: str, response: object) -> NetworkEvent:
    return NetworkEvent(
        id=f"net#planted-{n}",
        ts=ts,
        method="GET",
        url=url,
        status=200,
        resource_type="fetch",
        response_body=json.dumps(response),
        mime_type="application/json",
    )


def ending_on(
    run: Run,
    actions: list[ActionEvent],
    network: list[NetworkEvent],
    page: PageSnapshot,
    metadata: dict[str, object],
) -> Run:
    """`run` with events appended, ending on `page`."""
    return run.model_copy(
        update={
            "actions": [*run.actions, *actions],
            "network": [*run.network, *network],
            "snapshots": [*run.snapshots, page],
            "final_state": PageState.of(page),
            "metadata": metadata,
        }
    )


def confirmation_url(origin: str, run: Run, seed: int) -> str:
    reference = reference_of(run.agent_answer or "") or str(1000 + seed)
    return f"{origin}/orders/{reference}"


def first_path(urls: Sequence[str], default: str) -> str:
    path = next((u for u in urls if u.startswith("/")), default)
    return path


def order_request(
    url: str, ts: float, slug: str, run: Run, targets: Targets, *, status: int
) -> NetworkEvent:
    body: dict[str, object] = {"items": [{"product_id": slug, "qty": 1, "price": price_of(run)}]}
    if targets.body:
        body["note"] = " ".join(targets.body)
    return NetworkEvent(
        id="net#planted-1",
        ts=ts,
        method="POST",
        url=url,
        status=status,
        resource_type="fetch",
        request_headers={"Content-Type": "application/json"},
        request_body=json.dumps(body),
        mime_type="application/json",
    )
