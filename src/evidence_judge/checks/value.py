"""ValuePredicate checker.

Values are read from JSON (or form-encoded) bodies via `json_path` - from the requests matching
`in_request`, or from every mutation that took effect (2xx or 3xx, not failed) when it is
unset, so a rejected attempt cannot break a constraint - and/or from page text via `page_regex`:
the matches on every recorded page state (snapshots and the final page). Every observation must
satisfy the comparison: one violating observation makes the predicate CONTRADICTED, which is
what a guardrail needs, and satisfying observations that differ are noted. A page_regex should
therefore anchor on a label ('Total:\\s*\\$([\\d.,]+)'): a bare price pattern also reads
shipping fees, coupons and other products. A candidate whose body ingest truncated before the
path keeps the predicate from being SUPPORTED: the unread part could hold a violating value, so
the result is NO_EVIDENCE. So does a page state that page_regex could not read in full (over
MAX_REGEX_MATCHES matches, or text longer than the head and tail it searches): a violating value
found in the read part still contradicts. So is an ordering comparison (<, <=, >, >=) on a
value that does not parse as a number: text is never ordered.
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
    regex_findall,
    url_path,
)
from evidence_judge.checks.request import RequestMatcher, matching_requests
from evidence_judge.core.evidence import Channel, NetworkEvent
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import Predicate, ValuePredicate
from evidence_judge.core.verdict import Citation, PredicateResult, Support

KIND = "value"
MAX_LISTED_VALUES = 4


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
    return [e for e in index.mutations() if e.ok]


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
    return "No mutating request that took effect (2xx or 3xx) was captured"


def _from_pages(regex_source: str, index: EvidenceIndex) -> tuple[list[Observation], list[str]]:
    """Every match on every page state, and the ids of states whose text was not fully read."""
    regex = compile_regex(regex_source)
    if regex.groups != 1:
        raise InvalidPredicateError(f"page_regex /{regex_source}/ needs exactly one group")
    observations: list[Observation] = []
    partial: list[str] = []
    for state in index.page_states():
        evidence_id = state_id(state, index)
        found = regex_findall(regex, state.text)
        if not found.complete:
            partial.append(evidence_id)
        observations.extend(
            Observation(match.group(1), evidence_id, f"the page {url_path(state.url)}")
            for match in found.matches
        )
    return observations, partial


def _partial_reason(state_ids: list[str]) -> str:
    ids = ", ".join(state_ids[-MAX_CITATIONS:])
    return (
        f"{plural(len(state_ids), 'page state')} ({ids}) had more text or matches than "
        f"page_regex reads"
    )


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
        pages, partial = _from_pages(predicate.page_regex, index)
        observations.extend(pages)
        if partial:
            reason = _partial_reason(partial)
            unread = reason if unread is None else f"{unread}; {reason}"
            gaps.append(reason)
        elif not pages:
            gaps.append(f"No recorded page text matches /{predicate.page_regex}/")
    return observations, "; ".join(gaps), unread


def check_value(predicate: ValuePredicate, index: EvidenceIndex) -> PredicateResult:
    observations, gaps, unread = _observe(predicate, index)
    field = predicate.field
    expected = f"{predicate.op} {as_text(predicate.value)}"
    if not observations:
        return result(Support.NO_EVIDENCE, f"{gaps}, so {quoted(field)} could not be read", KIND)
    verdicts = [(o, compare(o.value, predicate.op, predicate.value)) for o in observations]
    violations = [o for o, holds in verdicts if holds is False]
    incomparable = [o for o, holds in verdicts if holds is None]
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
    if incomparable:
        odd = incomparable[-1]
        return result(
            Support.NO_EVIDENCE,
            f"{field} was {format_value(odd.value)} in {odd.where}: cannot compare "
            f"{format_value(odd.value)} numerically with {expected}",
            KIND,
            _cite(incomparable),
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
        f"{field} was {format_value(latest.value)} in {latest.where}, satisfying {expected}"
        f"{_disagreement(observations)}",
        KIND,
        _cite(observations),
    )


def _disagreement(observations: list[Observation]) -> str:
    values = list(dict.fromkeys(format_value(o.value) for o in observations))
    if len(values) < 2:
        return ""
    shown = ", ".join(values[:MAX_LISTED_VALUES])
    more = f" and {len(values) - MAX_LISTED_VALUES} more" if len(values) > MAX_LISTED_VALUES else ""
    return (
        f" ({plural(len(observations), 'observation')}, all satisfying it; observed values "
        f"differ: {shown}{more})"
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
