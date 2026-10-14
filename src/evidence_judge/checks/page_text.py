from __future__ import annotations

from evidence_judge.checks._common import (
    FINAL_ID,
    MAX_CITATIONS,
    plural,
    quoted,
    require,
    result,
    state_id,
)
from evidence_judge.checks.base import CheckContext
from evidence_judge.checks.matching import contains, excerpt, url_path
from evidence_judge.core.evidence import Channel
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import PageTextPredicate, Predicate, Scope
from evidence_judge.core.verdict import Citation, PredicateResult, Support

KIND = "page_text"


def check_page_text(predicate: PageTextPredicate, index: EvidenceIndex) -> PredicateResult:
    if predicate.scope is Scope.FINAL:
        return _check_final(predicate, index)
    return _check_any(predicate, index)


def _check_final(predicate: PageTextPredicate, index: EvidenceIndex) -> PredicateResult:
    phrase = quoted(predicate.contains)
    state = index.final
    if state is None:
        return result(
            Support.NO_EVIDENCE,
            f"Final page state was not captured, so it cannot show {phrase}",
            KIND,
        )
    if contains(state.text, predicate.contains):
        return result(
            Support.SUPPORTED,
            f"Final page ({url_path(state.url)}) contains {phrase}",
            KIND,
            [Citation(evidence_id=FINAL_ID, excerpt=excerpt(state.text, predicate.contains))],
        )
    reason = f"Final page text does not contain {phrase}; final URL is {url_path(state.url)}"
    if not index.has(Channel.FINAL_STATE):
        return result(Support.NO_EVIDENCE, f"{reason} (final page text not fully captured)", KIND)
    return result(
        Support.CONTRADICTED,
        reason,
        KIND,
        [Citation(evidence_id=FINAL_ID, excerpt=excerpt(state.text, ""))],
    )


def _check_any(predicate: PageTextPredicate, index: EvidenceIndex) -> PredicateResult:
    phrase = quoted(predicate.contains)
    states = index.page_states()
    hits = [s for s in states if contains(s.text, predicate.contains)]
    if hits:
        cited = list(reversed(hits[-MAX_CITATIONS:]))
        return result(
            Support.SUPPORTED,
            f"{phrase} appears on {plural(len(hits), 'page state')}, latest at "
            f"{url_path(cited[0].url)}",
            KIND,
            [
                Citation(
                    evidence_id=state_id(s, index), excerpt=excerpt(s.text, predicate.contains)
                )
                for s in cited
            ],
        )
    if not index.has(Channel.SNAPSHOTS):
        return result(
            Support.NO_EVIDENCE,
            f"Page snapshots were not captured, so no page can be shown to contain {phrase}",
            KIND,
        )
    where = f"; final URL is {url_path(index.final.url)}" if index.final else ""
    return result(
        Support.CONTRADICTED,
        f"None of {plural(len(states), 'recorded page state')} contains {phrase}{where}",
        KIND,
    )


class PageTextChecker:
    kind = KIND
    deterministic = True

    async def evaluate(self, predicate: Predicate, ctx: CheckContext) -> PredicateResult:
        return check_page_text(require(predicate, PageTextPredicate, self.kind), ctx.index)
