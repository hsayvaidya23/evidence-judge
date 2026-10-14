from __future__ import annotations


class CheckError(Exception):
    """Base class for errors raised while evaluating predicates."""


class InvalidPredicateError(CheckError):
    """The predicate cannot be evaluated as written (bad regex, malformed status, no criteria)."""


class UnknownPredicateKindError(CheckError):
    def __init__(self, kind: str) -> None:
        super().__init__(f"no checker registered for predicate kind {kind!r}")
        self.kind = kind


class PredicateKindMismatchError(CheckError):
    def __init__(self, checker: str, got: str) -> None:
        super().__init__(f"{checker} checker cannot evaluate a {got!r} predicate")
        self.checker = checker
        self.got = got


class DuplicateCheckerError(CheckError):
    def __init__(self, kind: str) -> None:
        super().__init__(f"a checker for predicate kind {kind!r} is already registered")
        self.kind = kind
