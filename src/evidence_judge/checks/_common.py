from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from pydantic import BaseModel

from evidence_judge.checks.errors import PredicateKindMismatchError
from evidence_judge.checks.matching import url_path
from evidence_judge.core.evidence import NetworkEvent, PageState
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import Predicate
from evidence_judge.core.verdict import Citation, PredicateResult, Support

FINAL_ID = "final"
MAX_CITATIONS = 3
CONFIDENT_CONTRADICTION: Final = 0.7
"""A key claim contradicted at or above this confidence fails the run on its own."""
WEAK_CONTRADICTION_CONFIDENCE: Final = 0.6
"""Confidence of contradictions that can only send a run to review: absence across point-in-time
snapshots, a vision model's "no", or conflicting evidence."""


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
    return f"{event.shown_method} {url_path(event.url)} → {status}"


def took_effect(event: NetworkEvent) -> bool:
    """A mutation that was not refused: not failed, and answered below 400 (or not at all)."""
    return event.is_mutation and not event.failed and (event.status is None or event.status < 400)


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


def lower_first(text: str) -> str:
    """`text` with its first letter lower-cased to continue a sentence, unless it starts an
    acronym or a name such as `POST` or `URL`."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def join_clauses(clauses: Iterable[str]) -> str:
    """Distinct clauses as one sentence: `; `-separated, later ones lower-cased."""
    unique = list(dict.fromkeys(clauses))
    return "; ".join(unique[:1] + [lower_first(c) for c in unique[1:]])


def join_reasons(results: Iterable[PredicateResult]) -> str:
    return join_clauses(r.reason for r in results)


def checker_names(results: Iterable[PredicateResult]) -> str:
    return "+".join(dict.fromkeys(r.checker for r in results))
