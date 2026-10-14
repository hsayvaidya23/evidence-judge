from __future__ import annotations

from evidence_judge.checks._common import (
    FINAL_ID,
    MAX_CITATIONS,
    WEAK_CONTRADICTION_CONFIDENCE,
    plural,
    quoted,
    require,
    result,
    state_id,
)
from evidence_judge.checks.base import CheckContext
from evidence_judge.checks.errors import InvalidPredicateError
from evidence_judge.checks.matching import compile_regex, regex_search, url_contains, url_path
from evidence_judge.core.evidence import Channel, NetworkEvent
from evidence_judge.core.index import EvidenceIndex, is_document_navigation
from evidence_judge.core.predicates import Predicate, Scope, UrlPredicate
from evidence_judge.core.verdict import Citation, PredicateResult, Support

KIND = "url"


class UrlMatcher:
    def __init__(self, predicate: UrlPredicate) -> None:
        if predicate.contains is None and predicate.regex is None:
            raise InvalidPredicateError("url predicate needs 'contains' or 'regex'")
        self.predicate = predicate
        self._regex = compile_regex(predicate.regex) if predicate.regex else None

    def describe(self) -> str:
        parts: list[str] = []
        if self.predicate.contains is not None:
            parts.append(f"contain {quoted(self.predicate.contains)}")
        if self.predicate.regex is not None:
            parts.append(f"match /{self.predicate.regex}/")
        return " and ".join(parts)

    def matches(self, url: str) -> bool:
        p = self.predicate
        if p.contains is not None and not url_contains(url, p.contains):
            return False
        return self._regex is None or regex_search(self._regex, url) is not None


def check_url(predicate: UrlPredicate, index: EvidenceIndex) -> PredicateResult:
    matcher = UrlMatcher(predicate)
    if predicate.scope is Scope.FINAL:
        return _check_final(matcher, index)
    return _check_any(matcher, index)


def _check_final(matcher: UrlMatcher, index: EvidenceIndex) -> PredicateResult:
    state = index.final
    if state is None:
        return result(
            Support.NO_EVIDENCE,
            f"Final page state was not captured, so its URL cannot be shown to {matcher.describe()}",
            KIND,
        )
    cite = [Citation(evidence_id=FINAL_ID, excerpt=state.url)]
    if matcher.matches(state.url):
        return result(
            Support.SUPPORTED,
            f"Final URL {url_path(state.url)} does {matcher.describe()}",
            KIND,
            cite,
        )
    return result(
        Support.CONTRADICTED,
        f"Final URL is {url_path(state.url)}, which does not {matcher.describe()}",
        KIND,
        cite,
    )


def _visited(index: EvidenceIndex, navigations: list[NetworkEvent]) -> list[tuple[str, str]]:
    states = [(state_id(s, index), s.url) for s in index.page_states()]
    actions = [(a.id, a.url) for a in index.run.actions if a.url]
    loads = [(n.id, n.url) for n in navigations]
    return [*actions, *loads, *states]


def _check_any(matcher: UrlMatcher, index: EvidenceIndex) -> PredicateResult:
    navigations = [n for n in index.requests("GET") if is_document_navigation(n)]
    visited = _visited(index, navigations)
    hits = [(eid, url) for eid, url in visited if matcher.matches(url)]
    if hits:
        cited = list(reversed(hits[-MAX_CITATIONS:]))
        return result(
            Support.SUPPORTED,
            f"Visited URL {url_path(cited[0][1])} does {matcher.describe()}",
            KIND,
            [Citation(evidence_id=eid, excerpt=url) for eid, url in cited],
        )
    if not (index.has(Channel.SNAPSHOTS) or (index.has(Channel.NETWORK) and navigations)):
        return result(
            Support.NO_EVIDENCE,
            f"Neither page snapshots nor page navigations were captured, so no visited URL can "
            f"be shown to {matcher.describe()}",
            KIND,
        )
    distinct = {url for _, url in visited}
    reason = f"None of {plural(len(distinct), 'visited URL')} does {matcher.describe()}"
    if navigations:
        return result(Support.CONTRADICTED, reason, KIND)
    return result(
        Support.CONTRADICTED,
        f"{reason} (no page navigations were captured and snapshots are point-in-time, so the "
        "URL may have been visited between them)",
        KIND,
        confidence=WEAK_CONTRADICTION_CONFIDENCE,
    )


class UrlChecker:
    kind = KIND
    deterministic = True

    async def evaluate(self, predicate: Predicate, ctx: CheckContext) -> PredicateResult:
        return check_url(require(predicate, UrlPredicate, self.kind), ctx.index)
