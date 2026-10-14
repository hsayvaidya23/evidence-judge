from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

from evidence_judge.checks.errors import PredicateKindMismatchError
from evidence_judge.checks.matching import url_path
from evidence_judge.core.evidence import NetworkEvent, PageState
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import Predicate
from evidence_judge.core.verdict import Citation, PredicateResult, Support

FINAL_ID = "final"
MAX_CITATIONS = 3


def require[P: BaseModel](predicate: Predicate, cls: type[P], checker: str) -> P:
    if not isinstance(predicate, cls):
        raise PredicateKindMismatchError(checker, predicate.kind)
    return predicate


def result(
    status: Support,
    reason: str,
    checker: str,
    citations: Iterable[Citation] = (),
    confidence: float = 1.0,
) -> PredicateResult:
    return PredicateResult(
        status=status,
        reason=reason,
        citations=list(citations),
        checker=checker,
        confidence=confidence,
    )


def quoted(text: str) -> str:
    return repr(text) if "'" not in text else f'"{text}"'


def describe_request(event: NetworkEvent) -> str:
    status = "no response" if event.status is None else str(event.status)
    if event.failed:
        status = f"{status}, failed"
    return f"{event.method.upper()} {url_path(event.url)} → {status}"


def cite_request(event: NetworkEvent) -> Citation:
    return Citation(evidence_id=event.id, excerpt=describe_request(event))


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def state_id(state: PageState, index: EvidenceIndex) -> str:
    if state is index.final or state.snapshot_id is None:
        return FINAL_ID
    return state.snapshot_id


def merge_citations(results: Iterable[PredicateResult]) -> list[Citation]:
    seen: dict[str, Citation] = {}
    for res in results:
        for citation in res.citations:
            seen.setdefault(citation.evidence_id, citation)
    return list(seen.values())


def join_reasons(results: Iterable[PredicateResult]) -> str:
    return "; ".join(dict.fromkeys(r.reason for r in results))


def checker_names(results: Iterable[PredicateResult]) -> str:
    return "+".join(dict.fromkeys(r.checker for r in results))
