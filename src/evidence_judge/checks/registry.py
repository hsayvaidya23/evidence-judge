from __future__ import annotations

from collections.abc import Iterable

from evidence_judge.checks._common import result
from evidence_judge.checks.action import ActionChecker
from evidence_judge.checks.base import CheckContext, PredicateChecker
from evidence_judge.checks.errors import (
    DuplicateCheckerError,
    InvalidPredicateError,
    UnknownPredicateKindError,
)
from evidence_judge.checks.page_text import PageTextChecker
from evidence_judge.checks.request import RequestChecker
from evidence_judge.checks.screenshot import ScreenshotChecker
from evidence_judge.checks.url import UrlChecker
from evidence_judge.checks.value import ValueChecker
from evidence_judge.core.predicates import Predicate
from evidence_judge.core.verdict import PredicateResult, Support


class CheckerRegistry:
    """Maps predicate kinds to checkers. A malformed predicate yields NO_EVIDENCE, never a crash."""

    def __init__(self, checkers: Iterable[PredicateChecker] = ()) -> None:
        self._checkers: dict[str, PredicateChecker] = {}
        for checker in checkers:
            self.register(checker)

    def register(self, checker: PredicateChecker) -> None:
        if checker.kind in self._checkers:
            raise DuplicateCheckerError(checker.kind)
        self._checkers[checker.kind] = checker

    def get(self, kind: str) -> PredicateChecker:
        try:
            return self._checkers[kind]
        except KeyError:
            raise UnknownPredicateKindError(kind) from None

    def is_deterministic(self, predicate: Predicate) -> bool:
        return self.get(predicate.kind).deterministic

    def model_kinds(self) -> frozenset[str]:
        """Kinds whose checkers call a model (non-deterministic)."""
        return frozenset(k for k, c in self._checkers.items() if not c.deterministic)

    async def evaluate(self, predicate: Predicate, ctx: CheckContext) -> PredicateResult:
        checker = self.get(predicate.kind)
        try:
            return await checker.evaluate(predicate, ctx)
        except InvalidPredicateError as exc:
            return result(Support.NO_EVIDENCE, f"Predicate cannot be checked: {exc}", checker.kind)


def default_registry(*, screenshots_enabled: bool = True) -> CheckerRegistry:
    return CheckerRegistry(
        [
            RequestChecker(),
            PageTextChecker(),
            UrlChecker(),
            ActionChecker(),
            ValueChecker(),
            ScreenshotChecker(enabled=screenshots_enabled),
        ]
    )
