from __future__ import annotations

from typing import Final

from evidence_judge.checks._common import MAX_CITATIONS, plural, quoted, require, result
from evidence_judge.checks.base import CheckContext
from evidence_judge.checks.errors import InvalidPredicateError
from evidence_judge.checks.matching import contains, normalize, url_path
from evidence_judge.core.evidence import ActionEvent, Channel
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import ActionPredicate, Predicate
from evidence_judge.core.verdict import Citation, PredicateResult, Support

KIND = "action"
FAILED_ATTEMPT_CONFIDENCE = 0.9
ACTION_ALIASES: Final = {
    "navigate": "goto",
    "visit": "goto",
    "open": "goto",
    "type": "fill",
    "input": "fill",
    "tap": "click",
    "press_key": "press",
}


def canonical_action(name: str) -> str:
    key = normalize(name).replace(" ", "_")
    return ACTION_ALIASES.get(key, key)


def action_target(action: ActionEvent) -> str | None:
    return action.selector or action.url


def describe_action(action: ActionEvent) -> str:
    target = action.selector or (url_path(action.url) if action.url else None)
    return f"{action.name} {target}" if target else action.name


class ActionMatcher:
    def __init__(self, predicate: ActionPredicate) -> None:
        if not (predicate.name or predicate.target_contains or predicate.value_contains):
            raise InvalidPredicateError(
                "action predicate needs 'name', 'target_contains' or 'value_contains'"
            )
        self.predicate = predicate
        self.name = canonical_action(predicate.name) if predicate.name else None

    def describe(self) -> str:
        p = self.predicate
        parts = [f"{self.name} action" if self.name else "action"]
        if p.target_contains:
            parts.append(f"whose target contains {quoted(p.target_contains)}")
        if p.value_contains:
            parts.append(f"with a value containing {quoted(p.value_contains)}")
        return " ".join(parts)

    def matches(self, action: ActionEvent) -> bool:
        p = self.predicate
        if self.name and canonical_action(action.name) != self.name:
            return False
        if p.target_contains and not contains(action_target(action), p.target_contains):
            return False
        return not (p.value_contains and not contains(action.value, p.value_contains))


def _cite(actions: list[ActionEvent]) -> list[Citation]:
    return [
        Citation(evidence_id=a.id, excerpt=describe_action(a))
        for a in reversed(actions[-MAX_CITATIONS:])
    ]


def check_action(predicate: ActionPredicate, index: EvidenceIndex) -> PredicateResult:
    matcher = ActionMatcher(predicate)
    actions = index.run.actions
    hits = [a for a in actions if matcher.matches(a)]
    done = [a for a in hits if a.error is None]
    if done:
        return result(
            Support.SUPPORTED,
            f"Performed {describe_action(done[-1])} ({plural(len(done), 'matching action')})",
            KIND,
            _cite(done),
        )
    if hits:
        last = hits[-1]
        return result(
            Support.CONTRADICTED,
            f"{describe_action(last)} was attempted {plural(len(hits), 'time')} but failed: "
            f"{last.error}",
            KIND,
            _cite(hits),
            confidence=FAILED_ATTEMPT_CONFIDENCE,
        )
    if not index.has(Channel.ACTIONS):
        return result(
            Support.NO_EVIDENCE,
            f"Browser actions were not captured, so no {matcher.describe()} can be confirmed",
            KIND,
        )
    reason = f"No {matcher.describe()} among {plural(len(actions), 'recorded action')}"
    if matcher.name:
        same = sum(1 for a in actions if canonical_action(a.name) == matcher.name)
        reason += f" ({plural(same, f'{matcher.name} action')})"
    return result(Support.CONTRADICTED, reason, KIND)


class ActionChecker:
    kind = KIND
    deterministic = True

    async def evaluate(self, predicate: Predicate, ctx: CheckContext) -> PredicateResult:
        return check_action(require(predicate, ActionPredicate, self.kind), ctx.index)
