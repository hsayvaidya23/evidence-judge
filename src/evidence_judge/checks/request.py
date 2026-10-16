from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final, cast
from urllib.parse import parse_qsl, unquote_plus, urljoin, urlsplit

import regex

from evidence_judge.checks._common import (
    MAX_CITATIONS,
    WEAK_CONTRADICTION_CONFIDENCE,
    cite_request,
    describe_request,
    plural,
    quoted,
    require,
    result,
)
from evidence_judge.checks.base import CheckContext, EvidenceScope
from evidence_judge.checks.matching import (
    body_values,
    compile_regex,
    contains,
    is_truncated,
    parse_body,
    regex_search,
    shows_amount,
    status_matches,
    url_host,
    url_location_contains,
    url_path_contains,
    url_query_contains,
    validate_status_spec,
)
from evidence_judge.core.actions import (
    CHECK,
    FILL,
    GOTO,
    HOVER,
    UNCHECK,
    UPLOAD,
    canonical_action,
)
from evidence_judge.core.evidence import Channel, NetworkEvent
from evidence_judge.core.index import EvidenceIndex, is_document_navigation
from evidence_judge.core.predicates import STATUS_ANY, Predicate, RequestPredicate
from evidence_judge.core.site import Site
from evidence_judge.core.verdict import PredicateResult, Support
from evidence_judge.textnorm import normalize


@dataclass(frozen=True, slots=True)
class Miss:
    """One unmet criterion. `unrecorded` means the evidence needed to decide it was not captured;
    `off_site` that the request went to a host outside the evidence scope; `unshown` that what
    the site answered (`site_result`) was recorded and does not show a `result_contains` text."""

    reason: str
    unrecorded: bool = False
    off_site: bool = False
    unshown: bool = False


class RequestMatcher:
    """Compiled form of a RequestPredicate. Raises InvalidPredicateError on a malformed one."""

    def __init__(
        self,
        predicate: RequestPredicate,
        scope: EvidenceScope | None = None,
        index: EvidenceIndex | None = None,
    ) -> None:
        validate_status_spec(predicate.status)
        self.predicate = predicate
        self.scope = scope
        self.index = index
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
            + len(p.result_contains)
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
        if p.result_contains:
            parts.append(
                "whose response or the page it led to shows "
                + " and ".join(quoted(r) for r in p.result_contains)
            )
        return " ".join([head, *parts])

    def misses(self, event: NetworkEvent) -> list[Miss]:
        p = self.predicate
        out: list[Miss] = []
        if p.method and p.method.upper() not in (event.method.upper(), event.write_method):
            out.append(Miss(f"method {event.shown_method}, expected {p.method.upper()}"))
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
        out.extend(self._result_misses(event))
        if self.scope is not None and not self.scope.request_allowed(event):
            out.append(
                Miss(
                    f"went to {url_host(event.url)}, outside {self.scope.describe()}", off_site=True
                )
            )
        return out

    def matches(self, event: NetworkEvent) -> bool:
        return not self.misses(event)

    def _result_misses(self, event: NetworkEvent) -> list[Miss]:
        needles = self.predicate.result_contains
        if not needles:
            return []
        shown = site_result(event, self.index, self.scope) if self.index is not None else None
        if shown is None:
            return [
                Miss(
                    "neither its response nor a page it led to before the agent acted again "
                    f"was recorded showing {quoted(n)}",
                    unrecorded=True,
                )
                for n in needles
                if not _shows(event.response_body, n)
            ]
        return [
            Miss(f"neither its response nor the page it led to shows {quoted(n)}", unshown=True)
            for n in needles
            if not any(_shows(text, n) for text in shown)
        ]


def _shows(text: str | None, needle: str) -> bool:
    """Whether `text` shows `needle`, as text or, in a JSON answer, as an amount under a price
    key (matching.shows_amount): an order answered `{"total": 29.5}` shows '$29.50'."""
    return contains(text, needle) or shows_amount(text, needle)


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
    matcher = RequestMatcher(predicate, scope, index)
    return [e for e in index.requests() if matcher.matches(e)]


Scored = list[tuple[NetworkEvent, list[Miss]]]


def check_request(
    predicate: RequestPredicate, index: EvidenceIndex, scope: EvidenceScope | None = None
) -> PredicateResult:
    matcher = RequestMatcher(predicate, scope, index)
    scored = [(event, matcher.misses(event)) for event in index.requests()]
    return (
        _hit_result(matcher, scored, index)
        or _unverifiable_result(matcher, scored)
        or _unshown_result(matcher, scored)
        or _off_site_result(matcher, scored)
        or _absent_result(matcher, scored, index)
    )


def _hit_result(
    matcher: RequestMatcher, scored: Scored, index: EvidenceIndex
) -> PredicateResult | None:
    """SUPPORTED by the matching requests, NO_EVIDENCE if every one is in doubt (a redirect that
    cannot be shown to have worked, a mutation whose response reports a failure), or None if no
    request matched."""
    hits = [event for event, misses in scored if not misses]
    if not hits:
        return None
    doubts = _doubts(hits, index, matcher.scope) if matcher.scope is not None else {}
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


def _doubts(hits: list[NetworkEvent], index: EvidenceIndex, scope: EvidenceScope) -> dict[str, str]:
    return {e.id: d for e in hits if (d := redirect_doubt(e, index, scope) or response_doubt(e))}


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


def _unshown_result(matcher: RequestMatcher, scored: Scored) -> PredicateResult | None:
    """A weak CONTRADICTED for the latest request that meets every criterion but shows the site
    did not answer with a `result_contains` text: a confirmation that leaves the price out
    refutes a claimed price only as far as review."""
    unshown = [(e, m) for e, m in scored if m and all(miss.unshown for miss in m)]
    if not unshown:
        return None
    event, misses = unshown[-1]
    return result(
        Support.CONTRADICTED,
        f"{describe_request(event)} meets every other criterion of a {matcher.describe()}, "
        f"but {' and '.join(m.reason for m in misses)}",
        RequestChecker.kind,
        [cite_request(event)],
        confidence=WEAK_CONTRADICTION_CONFIDENCE,
    )


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
        f"outside {where}, so it cannot prove anything; if the site owns {host} (an api. or "
        "static. subdomain), list it in the spec's allowed_hosts",
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


STILL_ACTIONS: Final = frozenset({FILL, HOVER, CHECK, UNCHECK, UPLOAD})
"""Actions that cannot take the browser to another page."""
MAX_OFF_SITE_HOPS: Final = 3
"""Off-site pages (a payment provider's) an order's answer may pass through on its way back."""
_LETTER_BEFORE = r"(?<![^\W\d_])"
_LETTER_AFTER = r"(?![^\W\d_])"
_URL_FAILURE = re.compile(
    _LETTER_BEFORE + r"(?:errors?|err|fail(?:s|ed|ures?)?|declin(?:e|es|ed)|refus(?:al|ed|e|ée?s?)?"
    r"|reject(?:s|ed|ion)?|cancel(?:l?ed|lation)|invalid|denied|unauthori[sz]ed|expired"
    r"|insufficient|try[\s_-]*again|problems?|unable|unsuccessful|timed?[\s_-]*out"
    r"|abgelehnt|fehl(?:er|geschlagen)?|rechaz(?:ad[oa]s?|o)|rifiut(?:at[oaie]|o)"
    r"|[ée]checs?|[ée]chou[ée]e?s?"
    r"|not[\s_-]+(?:be[\s_-]+)?(?:processed|completed|placed))" + _LETTER_AFTER,
    re.IGNORECASE,
)
_STATUS_KEY = re.compile(r"(?:status|state|result|outcome)$", re.IGNORECASE)
_STATUS_FAILURE: Final = frozenset({"ko", "nok", "nack"})
_SUCCESS_KEY = re.compile(r"^(?:ok|success|succeeded|successful|paid|approved)$", re.IGNORECASE)
_FALSE: Final = frozenset({"false", "0", "no"})
_TEXT_FAILURE = re.compile(
    r"\b(?:errors?|failed|failure|declined|refused|rejected|invalid|denied|unauthori[sz]ed"
    r"|expired|insufficient|try\s+again|unable\s+to|went\s+wrong"
    r"|(?:was|were|been|is|are)\s+cancell?ed"
    r"|(?:there\s+was|we\s+had|encountered)\s+an?\s+problem|a\s+problem\s+(?:occurred|with)"
    r"|not\s+(?:be\s+)?(?:processed|completed|placed))\b(?!-free)",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"\b(?:no|not|zero|0|without|never)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)
_UNSET = frozenset({"", "0", "false", "no", "none", "null", "off"})
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


def url_failure(url: str) -> str | None:
    """The failure marker in a URL's decoded path or query (`/checkout/failed`,
    `?payment=card_refused`, `paymentDeclined`, `?statut=refusé`, `?status=abgelehnt`), or None.

    Markers are whole words, in English and a few other languages. A query field whose value
    is unset (`?error=`, `?errors=0`) marks nothing; a status-like field (`status`, `state`,
    `result`, `outcome`, or a name ending in one) whose whole value is `ko` or `nok` marks a
    failure, and so does a success flag (`ok`, `success`, `paid`, ...) set to false, 0 or no.
    """
    parts = urlsplit(url)
    fields = [unquote_plus(parts.path)]
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        folded = value.strip().casefold()
        if _STATUS_KEY.search(key) and folded in _STATUS_FAILURE:
            return value
        if _SUCCESS_KEY.match(key) and folded in _FALSE:
            return f"{key}={value}"
        if folded not in _UNSET:
            fields.append(f"{key} {value}")
    for field in fields:
        if found := _URL_FAILURE.search(_CAMEL.sub(" ", field)):
            return found.group()
    return None


def text_failure(text: str) -> str | None:
    """The first failure message in page text ('Your card was declined', 'Please try again'),
    or None. A negated mention ('No errors', 'without errors', 'error-free') is not one."""
    for found in _TEXT_FAILURE.finditer(text):
        if not _NEGATED.search(text[max(0, found.start() - 40) : found.start()]):
            return found.group()
    return None


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


def _landing(event: NetworkEvent, index: EvidenceIndex) -> NetworkEvent | None:
    """The page load matching the redirect's Location header, else the next page load."""
    location = next((v for k, v in event.response_headers.items() if k.lower() == "location"), None)
    target = urljoin(event.url, location) if location else None
    loads = [n for n in index.requests("GET") if n.ts > event.ts and _page_load(n)]
    landing = next((n for n in loads if target and _same_page(n.url, target)), None)
    return landing or (loads[0] if loads else None)


def site_result(
    event: NetworkEvent, index: EvidenceIndex, scope: EvidenceScope | None = None
) -> list[str] | None:
    """What the site answered `event` with: its response body, and the page it led to
    (`_answer_page`) as its document and what it showed (`_landing_texts`); None when it led to
    no page that loaded with a 2xx, or nothing of that page was recorded."""
    landing = _answer_page(event, index, scope)
    if landing is None or landing.status is None or not 200 <= landing.status < 300:
        return None
    if landing.failed:
        return None
    page = [t for t in (landing.response_body, *_landing_texts(landing, index)) if t]
    return [event.response_body or "", *page] if page else None


def _answer_page(
    event: NetworkEvent, index: EvidenceIndex, scope: EvidenceScope | None
) -> NetworkEvent | None:
    """The page of the site `event` led to: the page its redirect named, else the next page load
    if the agent did nothing that can move the browser (an action but STILL_ACTIONS) before it.
    A page the agent opened itself is its choice, not the site's answer, however soon after the
    request it loads. A landing away from the site's own pages (a payment provider's 3-D Secure
    challenge, allowed or not) is passed through to the next page load, while the agent only
    acts on those pages and opens no URL itself."""
    landing = _landing(event, index)
    if landing is None:
        return None
    if not _redirected_to(event, landing) and _moved(index, event.ts, landing.ts, None):
        return None
    home = {Site.of_url(event.url), index.primary_site} - {None}
    for _ in range(MAX_OFF_SITE_HOPS):
        if _at_home(landing.url, home, scope):
            return landing
        after = next(
            (n for n in index.requests("GET") if n.ts > landing.ts and _page_load(n)), None
        )
        if after is None or _moved(index, landing.ts, after.ts, (home, scope)):
            return None
        landing = after
    return None


def _at_home(url: str | None, home: set[Site | None], scope: EvidenceScope | None) -> bool:
    """Whether `url` is a page of the site's own: on its scope and on the host the request went
    to or the run started on."""
    if url is None:
        return True
    if scope is not None and not scope.page_allowed(url):
        return False
    return not home or Site.of_url(url) in home


def _redirected_to(event: NetworkEvent, landing: NetworkEvent) -> bool:
    location = next((v for k, v in event.response_headers.items() if k.lower() == "location"), None)
    return location is not None and _same_page(landing.url, urljoin(event.url, location))


def _moved(
    index: EvidenceIndex,
    start: float,
    end: float,
    away: tuple[set[Site | None], EvidenceScope | None] | None,
) -> bool:
    """Whether the agent did something between `start` and `end` that can move the browser:
    any such action, or, while the browser is `away` from the site's own pages, opening a URL
    itself or acting on a page of the site's own."""
    for action in index.run.actions:
        if not start < action.ts < end or canonical_action(action.name) in STILL_ACTIONS:
            continue
        if away is None or canonical_action(action.name) == GOTO:
            return True
        if _at_home(action.url, *away):
            return True
    return False


def _origin(event: NetworkEvent, index: EvidenceIndex) -> str | None:
    """The page the request was sent from: the last page load before it, else the last action's."""
    earlier = [n.url for n in index.requests("GET") if n.ts < event.ts and _page_load(n)]
    earlier = earlier or [a.url for a in index.run.actions if a.url and a.ts < event.ts]
    return earlier[-1] if earlier else None


def _page_text(title: str | None, text: str) -> str:
    return " ".join(f"{title or ''} {text}".split())


def _landing_texts(landing: NetworkEvent, index: EvidenceIndex) -> list[str]:
    """What the landing page showed: each snapshot of it taken after it loaded and before the
    browser moved to another page, and the final state if the browser never moved on."""
    moved = [
        n.ts
        for n in index.requests("GET")
        if n.ts > landing.ts and _page_load(n) and not _same_page(n.url, landing.url)
    ]
    until = moved[0] if moved else math.inf
    texts = [
        _page_text(s.title, s.text)
        for s in index.run.snapshots
        if landing.ts <= s.ts < until and _same_page(s.url, landing.url)
    ]
    final = index.final
    if not moved and final is not None and _same_page(final.url, landing.url):
        texts.append(_page_text(final.title, final.text))
    return texts


def _text_before(origin: str, event: NetworkEvent, index: EvidenceIndex) -> str | None:
    shown = [s for s in index.run.snapshots if s.ts < event.ts and _same_page(s.url, origin)]
    return _page_text(shown[-1].title, shown[-1].text) if shown else None


def _marker(markers: tuple[regex.Pattern[str], ...], texts: list[str]) -> str | None:
    """The first match of one of the spec's `failure_markers` in `texts`, or None."""
    for text in texts:
        for marker in markers:
            if found := regex_search(marker, text):
                return found.group()
    return None


def _shows_success(landing: str, texts: list[str], scope: EvidenceScope | None) -> bool:
    """Whether the landing page, on the site, shows a page text the spec expects."""
    if scope is None or not scope.success_texts or not scope.page_allowed(landing):
        return False
    shown = [normalize(t) for t in texts]
    return any(normalize(p) in t for p in scope.success_texts for t in shown)


def redirect_doubt(
    event: NetworkEvent, index: EvidenceIndex, scope: EvidenceScope | None = None
) -> str | None:
    """Why a mutation answered with a redirect cannot be shown to have succeeded, or None.

    A 3xx answers a form that worked (POST/Redirect/GET) and one that failed (a redirect back to
    the cart with `?error=card_declined`) alike, so what it proves depends on the page it led to
    (the page load matching its Location header, else the next page load), and what that page
    showed before the browser moved on:

    - it must load with a 2xx, and neither its URL (url_failure) nor, for the spec's own
      `failure_markers` (regexes, any language), its URL or text may mark a failure;
    - if the landing page is on the site and shows a page text the spec's `expect` asks for
      (`scope.success_texts`, 'Thank you for your order'), that positive evidence decides: it
      proves the mutation, whatever else the page says ('if a payment is declined later, ...');
    - otherwise its whole text is scanned for a failure message (text_failure). Recorded page
      text is flat, so nothing says which part is a footer; scanning less would let a landing
      that says 'Your card was declined' prove the order. A spec that names the confirmation
      text avoids the scan;
    - a redirect back to the page the request was sent from (a settings form saving in place)
      also needs what that page then showed recorded, and different from before.

    Any other status returns None.
    """
    if event.status is None or not 300 <= event.status < 400:
        return None
    landing = _landing(event, index)
    answered = f"it was answered {event.status}"
    if landing is None:
        return f"{answered} and no page load after it was recorded, so where it led is unknown"
    led = f"{answered} and led to {describe_request(landing)}"
    if landing.status is None or not 200 <= landing.status < 300 or landing.failed:
        return f"{led}, which did not load"
    markers = scope.failure_markers if scope else ()
    if mark := url_failure(landing.url) or _marker(markers, [unquote_plus(landing.url)]):
        return f"{led}, whose URL marks a failure ({quoted(mark)})"
    texts = _landing_texts(landing, index)
    if mark := _marker(markers, texts):
        return f"{led}, whose page shows a failure marker of the spec ({quoted(mark)})"
    if _shows_success(landing.url, texts, scope):
        return None
    if mark := next(filter(None, map(text_failure, texts)), None):
        return f"{led}, whose page reports a failure ({quoted(mark)})"
    origin = _origin(event, index)
    if origin is None or not _same_page(origin, landing.url):
        return None
    back = f"{led}, back to the page it was sent from"
    if not texts:
        return f"{back}, and what that page showed was not recorded"
    if _text_before(origin, event, index) in texts:
        return f"{back}, which showed the same as before"
    return None


_FLAG_KEYS: Final = ("ok", "success", "succeeded")
_STATE_KEYS: Final = ("status", "state", "result", "outcome")


def _reported(value: object) -> bool:
    """Whether an `errors` or `error` field holds something: not null, empty or unset."""
    if isinstance(value, str):
        return value.strip().casefold() not in _UNSET
    if isinstance(value, bool):
        return value
    if isinstance(value, list | dict):
        return bool(cast(list[object] | dict[object, object], value))
    return value is not None


def _fields(value: object) -> dict[str, object] | None:
    """A JSON object's fields by case-folded name, or None for anything else."""
    if not isinstance(value, dict):
        return None
    return {str(k).casefold(): v for k, v in cast(dict[object, object], value).items()}


def _first_message(value: object) -> str:
    first: object = value
    if isinstance(value, list) and value:
        first = cast(list[object], value)[0]
    if (fields := _fields(first)) is not None:
        first = fields.get("message", fields.get("code", first))
    return str(first)[:80]


def _null_fields(data: object) -> list[str]:
    if not isinstance(data, dict):
        return []
    return [str(k) for k, v in cast(dict[object, object], data).items() if v is None]


def response_doubt(event: NetworkEvent) -> str | None:
    """Why a mutation answered without a redirect cannot be shown to have succeeded, or None.

    An API reports a failed write in the body of a 200 as often as with an error status: a
    GraphQL `{"data": {"placeOrder": null}, "errors": [...]}`, a REST `{"ok": false}`. A JSON
    (or form-encoded) response object whose top-level `errors` or `error` holds something,
    whose `ok`, `success` or `succeeded` is false, or whose `status`, `state`, `result` or
    `outcome` is a failure word ('failed', 'declined', 'error', ...) does not prove the
    mutation took effect. Requests that are not mutations, redirects and unrecorded or
    unparseable bodies return None.
    """
    if not event.is_mutation or event.status is None or 300 <= event.status < 400:
        return None
    parsed = parse_body(event.response_body)
    fields = _fields(parsed)
    if fields is None:
        return None
    answered = f"it was answered {event.status} and its response"
    values = _all_values(event.response_body, cast(dict[object, object], parsed))
    for key in ("errors", "error"):
        for value in values.get(key, []):
            if _reported(value):
                nulls = _null_fields(fields.get("data"))
                null = f", with {', '.join(map(quoted, nulls))} null" if nulls else ""
                return f"{answered} reports an error ({quoted(_first_message(value))}){null}"
    for key in _FLAG_KEYS:
        for value in values.get(key, []):
            if value is False or (isinstance(value, str) and value.strip().casefold() in _FALSE):
                return f"{answered} says {key}: false"
    for key in _STATE_KEYS:
        for value in values.get(key, []):
            if isinstance(value, str) and failure_word(value):
                return f"{answered} says {key}: {quoted(value)}"
    return None


def _all_values(body: str | None, parsed: dict[object, object]) -> dict[str, list[object]]:
    """Every value of each top-level key, by case-folded name: a key repeated in the body
    ('{"ok": false, "ok": true}') keeps all its values, since servers differ on which wins."""
    out: dict[str, list[object]] = {}
    for name in parsed:
        values = body_values(body, str(name)) or [parsed[name]]
        out.setdefault(str(name).casefold(), []).extend(values)
    return out


def failure_word(value: str) -> bool:
    """Whether a status-like value ('failed', 'DECLINED', 'payment_refused') names a failure."""
    return _URL_FAILURE.search(_CAMEL.sub(" ", value)) is not None


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
