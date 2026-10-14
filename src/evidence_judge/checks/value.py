"""ValuePredicate checker.

Values are read from JSON (or form-encoded) bodies via `json_path` - from the requests matching
`in_request`, or from every mutation when it is unset - and/or from page text via `page_regex`
on the most recent page state that matches. Every observation must satisfy the comparison: one
violating observation makes the predicate CONTRADICTED, which is what a guardrail needs. A
candidate whose body ingest truncated before the path keeps the predicate from being SUPPORTED:
the unread part could hold a violating value, so the result is NO_EVIDENCE.
"""

from __future__ import annotations

from dataclasses import dataclass

from evidence_judge.checks._common import (
    MAX_CITATIONS,
    describe_request,
    plural,
    quoted,
    require,
    result,
    state_id,
)
from evidence_judge.checks.base import CheckContext
from evidence_judge.checks.errors import InvalidPredicateError
from evidence_judge.checks.matching import (
    MISSING,
    as_text,
    compare,
    compile_regex,
    is_truncated,
    json_path_get,
    parse_body,
    parse_number,
    url_path,
)
from evidence_judge.checks.request import RequestMatcher, matching_requests
from evidence_judge.core.evidence import Channel, NetworkEvent
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import Predicate, ValuePredicate
from evidence_judge.core.verdict import Citation, PredicateResult, Support

KIND = "value"


@dataclass(frozen=True, slots=True)
class Observation:
    value: object
    evidence_id: str
    where: str


def format_value(value: object) -> str:
    number = parse_number(value)
    if number is not None and not isinstance(value, str):
        return repr(float(number))
    return quoted(as_text(value))


def _candidates(predicate: ValuePredicate, index: EvidenceIndex) -> list[NetworkEvent]:
    if predicate.in_request is not None:
        return matching_requests(predicate.in_request, index)
    return index.mutations()


def _read_bodies(event: NetworkEvent, path: str) -> Observation | None:
    for body, label in ((event.request_body, "request"), (event.response_body, "response")):
        value = json_path_get(parse_body(body), path)
        if value is not MISSING:
            return Observation(value, event.id, f"the {label} of {describe_request(event)}")
    return None


def _truncated_without(event: NetworkEvent, path: str) -> bool:
    bodies = (event.request_body, event.response_body)
    return any(is_truncated(b) for b in bodies) and _read_bodies(event, path) is None


def _truncated_reason(events: list[NetworkEvent], path: str) -> str:
    ids = ", ".join(e.id for e in events[-MAX_CITATIONS:])
    return (
        f"{plural(len(events), 'candidate request')} ({ids}) had a truncated body without "
        f"{quoted(path)} in its recorded part"
    )


def _missing_json_reason(
    predicate: ValuePredicate, path: str, candidates: list[NetworkEvent], index: EvidenceIndex
) -> str:
    if candidates:
        return (
            f"None of {plural(len(candidates), 'candidate request')} has {quoted(path)} in its body"
        )
    if not index.has(Channel.NETWORK):
        return "Network traffic was not captured"
    if predicate.in_request is not None:
        return f"No {RequestMatcher(predicate.in_request).describe()} was captured"
    return "No mutating request was captured"


def _from_pages(regex_source: str, index: EvidenceIndex) -> Observation | None:
    regex = compile_regex(regex_source)
    if regex.groups > 1:
        raise InvalidPredicateError(f"page_regex /{regex_source}/ must have at most one group")
    for state in reversed(index.page_states()):
        match = regex.search(state.text)
        if match:
            value = match.group(1) if regex.groups else match.group(0)
            return Observation(value, state_id(state, index), f"the page {url_path(state.url)}")
    return None


def _observe(
    predicate: ValuePredicate, index: EvidenceIndex
) -> tuple[list[Observation], str, str | None]:
    if predicate.json_path is None and predicate.page_regex is None:
        raise InvalidPredicateError("value predicate needs 'json_path' or 'page_regex'")
    observations: list[Observation] = []
    gaps: list[str] = []
    unread: str | None = None
    if predicate.json_path is not None:
        candidates = _candidates(predicate, index)
        found = [o for e in candidates if (o := _read_bodies(e, predicate.json_path)) is not None]
        observations.extend(found)
        truncated = [e for e in candidates if _truncated_without(e, predicate.json_path)]
        if truncated:
            unread = _truncated_reason(truncated, predicate.json_path)
            gaps.append(unread)
        elif not found:
            gaps.append(_missing_json_reason(predicate, predicate.json_path, candidates, index))
    if predicate.page_regex is not None:
        page = _from_pages(predicate.page_regex, index)
        if page is not None:
            observations.append(page)
        else:
            gaps.append(f"No recorded page text matches /{predicate.page_regex}/")
    return observations, "; ".join(gaps), unread


def check_value(predicate: ValuePredicate, index: EvidenceIndex) -> PredicateResult:
    observations, gaps, unread = _observe(predicate, index)
    field = predicate.field
    expected = f"{predicate.op} {as_text(predicate.value)}"
    if not observations:
        return result(Support.NO_EVIDENCE, f"{gaps}, so {quoted(field)} could not be read", KIND)
    violations = [o for o in observations if not compare(o.value, predicate.op, predicate.value)]
    if violations:
        worst = violations[-1]
        satisfied = len(observations) - len(violations)
        tail = f"; {plural(satisfied, 'other observation')} satisfied it" if satisfied else ""
        return result(
            Support.CONTRADICTED,
            f"{field} was {format_value(worst.value)} in {worst.where}, expected {expected}{tail}",
            KIND,
            _cite(violations),
        )
    latest = observations[-1]
    if unread is not None:
        return result(
            Support.NO_EVIDENCE,
            f"{field} was {format_value(latest.value)} in {latest.where}, but {unread}, so not "
            f"every observation can be shown to satisfy {expected}",
            KIND,
            _cite(observations),
        )
    return result(
        Support.SUPPORTED,
        f"{field} was {format_value(latest.value)} in {latest.where}, satisfying {expected}",
        KIND,
        _cite(observations),
    )


def _cite(observations: list[Observation]) -> list[Citation]:
    return [
        Citation(evidence_id=o.evidence_id, excerpt=f"{as_text(o.value)} ({o.where})")
        for o in reversed(observations[-MAX_CITATIONS:])
    ]


class ValueChecker:
    kind = KIND
    deterministic = True

    async def evaluate(self, predicate: Predicate, ctx: CheckContext) -> PredicateResult:
        return check_value(require(predicate, ValuePredicate, self.kind), ctx.index)
