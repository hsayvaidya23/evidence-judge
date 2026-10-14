from __future__ import annotations

from dataclasses import dataclass

from evidence_judge.checks._common import (
    MAX_CITATIONS,
    cite_request,
    describe_request,
    plural,
    quoted,
    require,
    result,
)
from evidence_judge.checks.base import CheckContext, EvidenceScope
from evidence_judge.checks.matching import (
    compile_regex,
    contains,
    is_truncated,
    regex_search,
    status_matches,
    url_host,
    url_location_contains,
    url_path_contains,
    url_query_contains,
    validate_status_spec,
)
from evidence_judge.core.evidence import Channel, NetworkEvent
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import STATUS_ANY, Predicate, RequestPredicate
from evidence_judge.core.verdict import PredicateResult, Support


@dataclass(frozen=True, slots=True)
class Miss:
    """One unmet criterion. `unrecorded` means the evidence needed to decide it was not captured."""

    reason: str
    unrecorded: bool = False


class RequestMatcher:
    """Compiled form of a RequestPredicate. Raises InvalidPredicateError on a malformed one."""

    def __init__(self, predicate: RequestPredicate, scope: EvidenceScope | None = None) -> None:
        validate_status_spec(predicate.status)
        self.predicate = predicate
        self.scope = scope
        self._regex = compile_regex(predicate.url_regex) if predicate.url_regex else None

    @property
    def criteria_count(self) -> int:
        p = self.predicate
        return (
            int(p.method is not None)
            + len(p.url_contains)
            + len(p.path_contains)
            + len(p.query_contains)
            + int(p.url_regex is not None)
            + int(p.status != STATUS_ANY)
            + len(p.body_contains)
            + len(p.response_contains)
        )

    def describe(self) -> str:
        p = self.predicate
        head = f"{p.method.upper()} request" if p.method else "request"
        parts: list[str] = []
        if p.url_contains:
            parts.append("whose URL contains " + " and ".join(quoted(u) for u in p.url_contains))
        if p.path_contains:
            parts.append("whose path contains " + " and ".join(quoted(u) for u in p.path_contains))
        if p.query_contains:
            parts.append(
                "whose query contains " + " and ".join(quoted(u) for u in p.query_contains)
            )
        if p.url_regex:
            parts.append(f"whose URL matches /{p.url_regex}/")
        if p.status != STATUS_ANY:
            parts.append(f"with {describe_status(p.status)}")
        if p.body_contains:
            parts.append(
                "with a request body containing " + " and ".join(quoted(b) for b in p.body_contains)
            )
        if p.response_contains:
            parts.append(
                "with a response containing " + " and ".join(quoted(r) for r in p.response_contains)
            )
        return " ".join([head, *parts])

    def misses(self, event: NetworkEvent) -> list[Miss]:
        p = self.predicate
        out: list[Miss] = []
        if p.method and event.method.upper() != p.method.upper():
            out.append(Miss(f"method {event.method.upper()}, expected {p.method.upper()}"))
        out.extend(
            Miss(f"URL lacks {quoted(u)}")
            for u in p.url_contains
            if not url_location_contains(event.url, u)
        )
        out.extend(
            Miss(f"path lacks {quoted(u)}")
            for u in p.path_contains
            if not url_path_contains(event.url, u)
        )
        out.extend(
            Miss(f"query lacks {quoted(u)}")
            for u in p.query_contains
            if not url_query_contains(event.url, u)
        )
        if self._regex and not regex_search(self._regex, event.url):
            out.append(Miss(f"URL does not match /{p.url_regex}/"))
        if not status_matches(event.status, p.status, failed=event.failed):
            out.append(Miss(f"{_got(event)}, expected {describe_status(p.status)}"))
        out.extend(_body_misses(event.request_body, p.body_contains, "request body"))
        out.extend(_body_misses(event.response_body, p.response_contains, "response body"))
        if self.scope is not None and not self.scope.request_allowed(event):
            out.append(Miss(f"went to {url_host(event.url)}, outside {self.scope.describe()}"))
        return out

    def matches(self, event: NetworkEvent) -> bool:
        return not self.misses(event)


def describe_status(spec: str | None) -> str:
    match spec:
        case None:
            return "any response"
        case "ok":
            return "a 2xx or 3xx status"
        case "none":
            return "no response"
        case "any":
            return "any outcome"
        case _:
            return f"status {spec}"


def _got(event: NetworkEvent) -> str:
    if event.status is None:
        return "no response"
    return f"status {event.status}, failed" if event.failed else f"status {event.status}"


def _body_misses(body: str | None, needles: list[str], label: str) -> list[Miss]:
    if not needles:
        return []
    if body is None:
        return [Miss(f"{label} not recorded", unrecorded=True)]
    absent = [n for n in needles if not contains(body, n)]
    if is_truncated(body):
        return [Miss(f"{label} was truncated before {quoted(n)}", unrecorded=True) for n in absent]
    return [Miss(f"{label} lacks {quoted(n)}") for n in absent]


def matching_requests(predicate: RequestPredicate, index: EvidenceIndex) -> list[NetworkEvent]:
    matcher = RequestMatcher(predicate)
    return [e for e in index.requests() if matcher.matches(e)]


def check_request(
    predicate: RequestPredicate, index: EvidenceIndex, scope: EvidenceScope | None = None
) -> PredicateResult:
    matcher = RequestMatcher(predicate, scope)
    requests = index.requests()
    scored = [(event, matcher.misses(event)) for event in requests]
    hits = [event for event, misses in scored if not misses]
    if hits:
        latest = hits[-1]
        extra = f" ({len(hits)} matching requests)" if len(hits) > 1 else ""
        return result(
            Support.SUPPORTED,
            f"{describe_request(latest)} matched: {matcher.describe()}{extra}",
            RequestChecker.kind,
            [cite_request(e) for e in reversed(hits[-MAX_CITATIONS:])],
        )

    unverifiable = [
        (event, misses) for event, misses in scored if all(m.unrecorded for m in misses)
    ]
    if unverifiable:
        event, misses = unverifiable[-1]
        missing = " and ".join(m.reason for m in misses)
        return result(
            Support.NO_EVIDENCE,
            f"{describe_request(event)} meets every other criterion of a {matcher.describe()}, "
            f"but {missing}",
            RequestChecker.kind,
            [cite_request(event)],
        )

    if not index.has(Channel.NETWORK):
        return result(
            Support.NO_EVIDENCE,
            f"Network traffic was not captured, so a {matcher.describe()} cannot be checked",
            RequestChecker.kind,
        )

    mutations = sum(1 for e in requests if e.is_mutation)
    reason = (
        f"No {matcher.describe()} among {plural(len(requests), 'captured request')} "
        f"({plural(mutations, 'mutation')})"
    )
    nearest = _nearest_miss(scored, matcher.criteria_count)
    if nearest is None:
        return result(Support.CONTRADICTED, reason, RequestChecker.kind)
    event, misses = nearest
    return result(
        Support.CONTRADICTED,
        f"{reason}; closest was {describe_request(event)} ({'; '.join(m.reason for m in misses)})",
        RequestChecker.kind,
        [cite_request(event)],
    )


def _nearest_miss(
    scored: list[tuple[NetworkEvent, list[Miss]]], criteria: int
) -> tuple[NetworkEvent, list[Miss]] | None:
    candidates = [(e, m) for e, m in scored if len(m) < criteria]
    if not candidates:
        return None
    fewest = min(len(m) for _, m in candidates)
    return [c for c in candidates if len(c[1]) == fewest][-1]


class RequestChecker:
    kind = "request"
    deterministic = True

    async def evaluate(self, predicate: Predicate, ctx: CheckContext) -> PredicateResult:
        return check_request(require(predicate, RequestPredicate, self.kind), ctx.index, ctx.scope)
