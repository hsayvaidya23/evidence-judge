"""ValuePredicate checker.

Values are read via `json_path` from both bodies - the request's (JSON or form-encoded) and the
response's (JSON, or form-encoded when its MIME type says so) - of the requests matching
`in_request`, or of every mutation that took effect (2xx or 3xx, not failed) when it is unset,
so a rejected attempt cannot break a constraint; and/or from page text via `page_regex`: the
matches on every recorded page state (snapshots and the final page). Every observation must
satisfy the comparison: one violating observation makes the predicate CONTRADICTED, which is
what a guardrail needs, and satisfying observations that differ are noted. So a request that
asked for a total of 20 answered with a total of 999 violates 'total <= 30', and a path should
name the field it means in both bodies (an object or array in the response where the request
sent a plain value is another field, and is not read). A page_regex should likewise anchor on a label
('Total:\\s*\\$([\\d.,]+)'): a bare price pattern also reads shipping fees, coupons and other
products.

A body that repeats the key ('price=999&price=20', '{"price": 999, "price": 20}') names every
value; servers differ on which one they use. If all of them violate, that contradicts; if only
some do, the result is NO_EVIDENCE, since the record does not show which value took effect.

A candidate with a truncated body keeps the predicate from being SUPPORTED: the unread part could
hold a violating (or repeated) value, so the result is NO_EVIDENCE; only a response recorded as
HTML or XML, which json_path never reads, is exempt. So does a page state that page_regex could
not read in full (over MAX_REGEX_MATCHES matches, or text longer than the head and tail it
searches). A violating value found in the read part still contradicts. So is an ordering
comparison (<, <=, >, >=) on a value that does not parse as a number: text is never ordered.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Final

from evidence_judge.checks._common import (
    MAX_CITATIONS,
    describe_request,
    join_clauses,
    plural,
    quoted,
    require,
    result,
    state_id,
)
from evidence_judge.checks.base import CheckContext, EvidenceScope
from evidence_judge.checks.errors import InvalidPredicateError
from evidence_judge.checks.matching import (
    as_text,
    body_values,
    compare,
    compile_regex,
    is_truncated,
    parse_number,
    regex_findall,
    url_path,
)
from evidence_judge.checks.page_text import MAY_HIDE
from evidence_judge.checks.request import RequestMatcher, matching_requests
from evidence_judge.core.echo import MASK
from evidence_judge.core.evidence import Channel, NetworkEvent
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import Predicate, ValuePredicate
from evidence_judge.core.verdict import Citation, PredicateResult, Support

KIND = "value"
MAX_LISTED_VALUES = 4
FORM_MIME: Final = "application/x-www-form-urlencoded"
MARKUP_MIMES: Final = frozenset(
    {"text/html", "application/xhtml+xml", "text/xml", "application/xml"}
)


@dataclass(frozen=True, slots=True)
class Observation:
    value: object
    evidence_id: str
    where: str
    repeated: bool = False


def format_value(value: object) -> str:
    number = parse_number(value)
    if number is not None and not isinstance(value, str):
        return repr(float(number))
    return quoted(as_text(value))


def _candidates(
    predicate: ValuePredicate, index: EvidenceIndex, scope: EvidenceScope | None
) -> list[NetworkEvent]:
    if predicate.in_request is not None:
        return matching_requests(predicate.in_request, index, scope)
    return [e for e in index.mutations() if e.ok and (scope is None or scope.request_allowed(e))]


def _essence(mime_type: str | None) -> str:
    return (mime_type or "").split(";", 1)[0].strip().lower()


def _bodies(event: NetworkEvent) -> list[tuple[str, str | None, bool]]:
    """(label, body, whether a form-encoded body is read) for both bodies of `event`."""
    form_response = _essence(event.mime_type) == FORM_MIME
    return [("request", event.request_body, True), ("response", event.response_body, form_response)]


def _read_bodies(event: NetworkEvent, path: str) -> list[Observation]:
    """Every value `path` names in either body of `event`.

    A value holding MASK is the site repeating the agent's own input
    (EvidenceIndex.without_agent_input): no reading at all. Where the request sent a plain
    value, an object or array at the same path of the response is another field under the same
    name (`plan: "team"` answered with `plan: {"id": ...}`), not a reading of this one.
    """
    observations: list[Observation] = []
    sent_plain = False
    for label, body, form in _bodies(event):
        values = body_values(body, path, form=form)
        if label == "request":
            sent_plain = bool(values) and not any(_structured(v) for v in values)
        elif sent_plain:
            values = [v for v in values if not _structured(v)]
        where = f"the {label} of {describe_request(event)}"
        repeated = len(values) > 1
        observations.extend(
            Observation(v, event.id, where, repeated) for v in values if MASK not in as_text(v)
        )
    return observations


def _structured(value: object) -> bool:
    return isinstance(value, dict | list)


def _truncated(event: NetworkEvent) -> bool:
    """A body of `event` was cut short and the cut part could hold a value json_path names."""
    request, response = event.request_body, event.response_body
    markup = _essence(event.mime_type) in MARKUP_MIMES
    return is_truncated(request) or (is_truncated(response) and not markup)


def _truncated_reason(events: list[NetworkEvent]) -> str:
    ids = ", ".join(e.id for e in events[-MAX_CITATIONS:])
    return f"{plural(len(events), 'candidate request')} ({ids}) had a truncated body"


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


def _from_pages(
    regex_source: str, index: EvidenceIndex, scope: EvidenceScope | None
) -> tuple[list[Observation], list[str], list[Observation]]:
    """Every match on every page state in `scope`, the ids of states whose text was not fully
    read, and the matches in text the page may hide (PageSnapshot.unsure_text)."""
    regex = compile_regex(regex_source)
    if regex.groups != 1:
        raise InvalidPredicateError(f"page_regex /{regex_source}/ needs exactly one group")
    observations: list[Observation] = []
    partial: list[str] = []
    unsure: list[Observation] = []
    for state in index.page_states():
        if scope is not None and not scope.page_allowed(state.url):
            continue
        evidence_id = state_id(state, index)
        found = regex_findall(regex, state.text)
        if not found.complete:
            partial.append(evidence_id)
        observations.extend(
            Observation(match.group(1), evidence_id, f"the page {url_path(state.url)}")
            for match in found.matches
        )
        hidden = regex_findall(regex, state.unsure_text) if state.unsure_text else None
        if hidden is not None and not hidden.complete:
            partial.append(evidence_id)
        unsure.extend(
            Observation(match.group(1), evidence_id, f"{MAY_HIDE} on {url_path(state.url)}")
            for match in (hidden.matches if hidden is not None else ())
        )
    return observations, partial, unsure


def _partial_reason(state_ids: list[str]) -> str:
    ids = ", ".join(state_ids[-MAX_CITATIONS:])
    return (
        f"{plural(len(state_ids), 'page state')} ({ids}) had more text or matches than "
        f"page_regex reads"
    )


def _observe(
    predicate: ValuePredicate, index: EvidenceIndex, scope: EvidenceScope | None
) -> tuple[list[Observation], str, str | None, list[Observation]]:
    if predicate.json_path is None and predicate.page_regex is None:
        raise InvalidPredicateError("value predicate needs 'json_path' or 'page_regex'")
    observations: list[Observation] = []
    gaps: list[str] = []
    unread: str | None = None
    unsure: list[Observation] = []
    if predicate.json_path is not None:
        candidates = _candidates(predicate, index, scope)
        found = [o for e in candidates for o in _read_bodies(e, predicate.json_path)]
        observations.extend(found)
        truncated = [e for e in candidates if _truncated(e)]
        if truncated:
            unread = _truncated_reason(truncated)
            gaps.append(unread)
        elif not found:
            gaps.append(_missing_json_reason(predicate, predicate.json_path, candidates, index))
    if predicate.page_regex is not None:
        pages, partial, unsure = _from_pages(predicate.page_regex, index, scope)
        observations.extend(pages)
        if partial:
            reason = _partial_reason(partial)
            unread = reason if unread is None else join_clauses((unread, reason))
            gaps.append(reason)
        elif not pages:
            gaps.append(f"No recorded page text matches /{predicate.page_regex}/")
    return observations, join_clauses(gaps), unread, unsure


def check_value(
    predicate: ValuePredicate, index: EvidenceIndex, scope: EvidenceScope | None = None
) -> PredicateResult:
    observations, gaps, unread, unsure = _observe(predicate, index, scope)
    field = predicate.field
    expected = f"{predicate.op} {as_text(predicate.value)}"
    if not observations:
        if unsure:
            return _unsure_result(field, unsure, f"so {quoted(field)} could not be read")
        return result(Support.NO_EVIDENCE, f"{gaps}, so {quoted(field)} could not be read", KIND)
    verdicts = [(o, compare(o.value, predicate.op, predicate.value)) for o in observations]
    violations, ambiguous = _violations(verdicts)
    doubtful = [o for o in unsure if compare(o.value, predicate.op, predicate.value) is not True]
    if doubtful:
        return _unsure_result(
            field, doubtful, f"and would not be shown to satisfy {expected} if the page shows it"
        )
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
    if ambiguous:
        worst, group = ambiguous
        shown = ", ".join(format_value(o.value) for o in group[:MAX_LISTED_VALUES])
        return result(
            Support.NO_EVIDENCE,
            f"{field} is repeated in {worst.where} with differing values ({shown}) and "
            f"{format_value(worst.value)} would violate {expected}: the record does not show "
            f"which one the server used",
            KIND,
            _cite(group),
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


def _unsure_result(field: str, unsure: list[Observation], tail: str) -> PredicateResult:
    latest = unsure[-1]
    return result(
        Support.NO_EVIDENCE,
        f"{field} was {format_value(latest.value)} in {latest.where}, {tail}: its styles may "
        "keep it from showing, and the judge cannot tell whether they do",
        KIND,
        _cite(unsure),
    )


def _violations(
    verdicts: list[tuple[Observation, bool | None]],
) -> tuple[list[Observation], tuple[Observation, list[Observation]] | None]:
    """The violations that decide, and the last repeated key whose values only partly violate.

    The values of a repeated key are one group: it violates for certain only if every value in
    it does, since the server used one of them and the record does not show which.
    """
    groups: defaultdict[tuple[str, str], list[tuple[Observation, bool | None]]] = defaultdict(list)
    for observation, holds in verdicts:
        if observation.repeated:
            groups[observation.evidence_id, observation.where].append((observation, holds))
    decided: list[Observation] = []
    ambiguous: tuple[Observation, list[Observation]] | None = None
    for observation, holds in verdicts:
        if holds is not False:
            continue
        if not observation.repeated:
            decided.append(observation)
            continue
        group = groups[observation.evidence_id, observation.where]
        if all(h is False for _, h in group):
            decided.append(observation)
        else:
            ambiguous = (observation, [o for o, _ in group])
    return decided, ambiguous


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
        return check_value(require(predicate, ValuePredicate, self.kind), ctx.index, ctx.scope)
