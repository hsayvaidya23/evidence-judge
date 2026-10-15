from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote_plus, urljoin, urlsplit

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
    url_path,
    url_path_contains,
    url_query_contains,
    validate_status_spec,
)
from evidence_judge.core.evidence import Channel, NetworkEvent
from evidence_judge.core.index import EvidenceIndex, is_document_navigation
from evidence_judge.core.predicates import STATUS_ANY, Predicate, RequestPredicate
from evidence_judge.core.verdict import PredicateResult, Support


@dataclass(frozen=True, slots=True)
class Miss:
    """One unmet criterion. `unrecorded` means the evidence needed to decide it was not captured;
    `off_site` that the request went to a host outside the evidence scope."""

    reason: str
    unrecorded: bool = False
    off_site: bool = False


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
            out.append(
                Miss(
                    f"went to {url_host(event.url)}, outside {self.scope.describe()}", off_site=True
                )
            )
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


def matching_requests(
    predicate: RequestPredicate, index: EvidenceIndex, scope: EvidenceScope | None = None
) -> list[NetworkEvent]:
    matcher = RequestMatcher(predicate, scope)
    return [e for e in index.requests() if matcher.matches(e)]


Scored = list[tuple[NetworkEvent, list[Miss]]]


def check_request(
    predicate: RequestPredicate, index: EvidenceIndex, scope: EvidenceScope | None = None
) -> PredicateResult:
    matcher = RequestMatcher(predicate, scope)
    scored = [(event, matcher.misses(event)) for event in index.requests()]
    return (
        _hit_result(matcher, scored, index)
        or _unverifiable_result(matcher, scored)
        or _off_site_result(matcher, scored)
        or _absent_result(matcher, scored, index)
    )


def _hit_result(
    matcher: RequestMatcher, scored: Scored, index: EvidenceIndex
) -> PredicateResult | None:
    """SUPPORTED by the matching requests, NO_EVIDENCE if every one was a redirect that cannot be
    shown to have worked, or None if no request matched."""
    hits = [event for event, misses in scored if not misses]
    if not hits:
        return None
    doubts = _redirect_doubts(hits, index) if matcher.scope is not None else {}
    proven = [e for e in hits if e.id not in doubts]
    if not proven:
        event = hits[-1]
        return _no_evidence(matcher, event, doubts[event.id])
    latest = proven[-1]
    extra = f" ({len(proven)} matching requests)" if len(proven) > 1 else ""
    return result(
        Support.SUPPORTED,
        f"{describe_request(latest)} matched: {matcher.describe()}{extra}",
        RequestChecker.kind,
        [cite_request(e) for e in reversed(proven[-MAX_CITATIONS:])],
    )


def _redirect_doubts(hits: list[NetworkEvent], index: EvidenceIndex) -> dict[str, str]:
    return {e.id: d for e in hits if (d := redirect_doubt(e, index))}


def _no_evidence(matcher: RequestMatcher, event: NetworkEvent, but: str) -> PredicateResult:
    return result(
        Support.NO_EVIDENCE,
        f"{describe_request(event)} meets every other criterion of a {matcher.describe()}, "
        f"but {but}",
        RequestChecker.kind,
        [cite_request(event)],
    )


def _unverifiable_result(matcher: RequestMatcher, scored: Scored) -> PredicateResult | None:
    """NO_EVIDENCE for the latest request whose only misses are criteria that were not recorded."""
    unverifiable = [(e, m) for e, m in scored if all(miss.unrecorded for miss in m)]
    if not unverifiable:
        return None
    event, misses = unverifiable[-1]
    return _no_evidence(matcher, event, " and ".join(m.reason for m in misses))


def _off_site_result(matcher: RequestMatcher, scored: Scored) -> PredicateResult | None:
    """NO_EVIDENCE for the latest request that would match if it had gone to the site."""
    off_site = [(e, m) for e, m in scored if _only_off_site(m)]
    if not off_site:
        return None
    event, misses = off_site[-1]
    host = url_host(event.url)
    unread = [m.reason for m in misses if not m.off_site]
    but = f" (and {' and '.join(unread)})" if unread else ""
    where = matcher.scope.describe() if matcher.scope else "the site"
    return result(
        Support.NO_EVIDENCE,
        f"{describe_request(event)} is a {matcher.describe()}{but}, but it went to {host}, "
        f"outside {where}, so it cannot prove anything; if {host} is part of the site, "
        "list it in the spec's allowed_hosts",
        RequestChecker.kind,
        [cite_request(event)],
    )


def _absent_result(
    matcher: RequestMatcher, scored: Scored, index: EvidenceIndex
) -> PredicateResult:
    """CONTRADICTED when traffic was captured and nothing matched, citing the nearest miss."""
    if not index.has(Channel.NETWORK):
        return result(
            Support.NO_EVIDENCE,
            f"Network traffic was not captured, so a {matcher.describe()} cannot be checked",
            RequestChecker.kind,
        )
    mutations = sum(1 for e, _ in scored if e.is_mutation)
    reason = (
        f"No {matcher.describe()} among {plural(len(scored), 'captured request')} "
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


_FAILURE_MARK = re.compile(
    r"(?<![a-z])(?:errors?|err|declined?|fail(?:s|ed|ure)?|invalid|denied|rejected)(?![a-z])",
    re.IGNORECASE,
)


def _page_load(event: NetworkEvent) -> bool:
    """A document GET, or a GET whose type was not recorded (it may be one)."""
    if event.method.upper() != "GET":
        return False
    if event.resource_type or event.mime_type:
        return is_document_navigation(event)
    return True


def _same_page(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.hostname, pa.path.rstrip("/")) == (pb.hostname, pb.path.rstrip("/"))


def redirect_doubt(event: NetworkEvent, index: EvidenceIndex) -> str | None:
    """Why a mutation answered with a redirect cannot be shown to have succeeded, or None.

    A 3xx answers a form that worked (POST/Redirect/GET) and one that failed (a redirect back to
    the cart with `?error=card_declined`) alike, so it proves the mutation took effect only when
    the page it led to (the page load matching its Location header, else the next page load)
    loaded with a 2xx, carries no error marker in its URL and is not the page the request was
    sent from (the last page load before it, else the last action's page). Any other status
    returns None.
    """
    if event.status is None or not 300 <= event.status < 400:
        return None
    location = next((v for k, v in event.response_headers.items() if k.lower() == "location"), None)
    target = urljoin(event.url, location) if location else None
    loads = [n for n in index.requests("GET") if n.ts > event.ts and _page_load(n)]
    landing = next((n for n in loads if target and _same_page(n.url, target)), None)
    landing = landing or (loads[0] if loads else None)
    answered = f"it was answered {event.status}"
    if landing is None:
        return f"{answered} and no page load after it was recorded, so where it led is unknown"
    led = f"{answered} and led to {describe_request(landing)}"
    if landing.status is None or not 200 <= landing.status < 300 or landing.failed:
        return f"{led}, which did not load"
    if _FAILURE_MARK.search(unquote_plus(url_path(landing.url))):
        return f"{led}, whose URL marks a failure"
    earlier = [n.url for n in index.requests("GET") if n.ts < event.ts and _page_load(n)]
    earlier = earlier or [a.url for a in index.run.actions if a.url and a.ts < event.ts]
    origin = earlier[-1] if earlier else None
    if origin is not None and _same_page(origin, landing.url):
        return f"{led}, back to the page it was sent from"
    return None


def _only_off_site(misses: list[Miss]) -> bool:
    """True if the request meets every criterion except the host scope (and ones not recorded)."""
    return any(m.off_site for m in misses) and all(m.off_site or m.unrecorded for m in misses)


def _nearest_miss(scored: Scored, criteria: int) -> tuple[NetworkEvent, list[Miss]] | None:
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
