"""What every session checker shares: the run outcome type, citations and results, and
glob matching of session paths."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from evidence_judge.core.verdict import Citation, PredicateResult, Support
from evidence_judge.sessions.edits import NO_DIFF
from evidence_judge.sessions.models import CommandEvent, SessionEvent

CHECKER: Final = "session"
EXCERPT: Final = 160


class Result(StrEnum):
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    event: CommandEvent
    result: Result
    reason: str
    partial: str | None = None
    """Why the run is not the whole suite, or None when it is."""

    @property
    def whole(self) -> bool:
        return self.partial is None


def blocker(finding: str) -> str:
    return "the code could not be checked" if NO_DIFF in finding else "the suite was weakened"


_ONE: Final = "?"
_SEGMENT: Final = "*"
_ANY: Final = "**"
_DIRS: Final = "**/"


def _glob_tokens(pattern: str) -> tuple[str, ...]:
    tokens: list[str] = []
    i = 0
    while i < len(pattern):
        token = next(
            (t for t in (_DIRS, _ANY, _SEGMENT, _ONE) if pattern.startswith(t, i)), pattern[i]
        )
        tokens.append(token)
        i += len(token)
    return tuple(tokens)


def _glob_fullmatch(tokens: tuple[str, ...], text: str) -> bool:
    """Whether `tokens` match all of `text`, by stepping every live position at once.

    Each character advances the whole set of pattern positions, so the time is the pattern
    length times the text length whatever the glob: a spec glob cannot make it backtrack.
    `**/` matches nothing or anything ending in '/', `**` anything, `*` and `?` anything
    within a path segment.
    """
    end = len(tokens)

    def closure(states: set[int]) -> set[int]:
        stack = list(states)
        while stack:
            i = stack.pop()
            if i < end and tokens[i] in (_DIRS, _ANY, _SEGMENT) and i + 1 not in states:
                states.add(i + 1)
                stack.append(i + 1)
        return states

    states = closure({0})
    inside: set[int] = set()
    for char in text:
        after: set[int] = set()
        within: set[int] = set()
        for i in states | inside:
            if i >= end:
                continue
            token = tokens[i]
            if token == _DIRS:
                within.add(i)
                if char == "/":
                    after.add(i + 1)
            elif token == _ANY or (token == _SEGMENT and char != "/"):
                after.add(i)
            elif (token == _ONE and char != "/") or token == char:
                after.add(i + 1)
        states, inside = closure(after), within
        if not states and not inside:
            return False
    return end in states


def path_matches(pattern: str, path: str) -> bool:
    """Gitignore-like: a pattern without '/' matches a file or directory name at any depth; a
    pattern with '/' matches from the session directory, and a directory pattern matches
    everything under it. Absolute paths outside the session directory match by suffix."""
    norm = posixpath.normpath(path).removeprefix("./")
    pat = pattern.rstrip("/")
    parts = [part for part in norm.split("/") if part]
    tokens = _glob_tokens(pat)
    if "/" not in pat:
        return any(_glob_fullmatch(tokens, part) for part in parts)
    under = _glob_tokens(pat + "/**")
    candidates = [norm]
    if norm.startswith(("/", "../")):
        candidates.extend("/".join(parts[i:]) for i in range(1, len(parts)))
    return any(_glob_fullmatch(tokens, c) or _glob_fullmatch(under, c) for c in candidates)


def cite(event: SessionEvent, excerpt: str | None = None) -> Citation:
    return Citation(evidence_id=event.id, excerpt=(excerpt or "")[:EXCERPT] or None)


def result(status: Support, reason: str, *events: SessionEvent) -> PredicateResult:
    citations = [
        cite(e, e.command if isinstance(e, CommandEvent) else getattr(e, "path", None))
        for e in {event.id: event for event in events}.values()
    ]
    return PredicateResult(status=status, reason=reason, citations=citations, checker=CHECKER)
