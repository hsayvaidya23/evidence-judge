"""What every session checker shares: the run outcome type, citations and results, and
glob matching of session paths."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from evidence_judge.core.verdict import Citation, PredicateResult, Support
from evidence_judge.sessions.edits import NO_DIFF
from evidence_judge.sessions.models import CallStatus, CommandEvent, SessionEvent, SessionRun
from evidence_judge.sessions.normalize import relative

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


_ROOT_PRINTERS: Final = ("pwd", "git rev-parse --show-toplevel")


def _single(values: list[str]) -> str | None:
    distinct = {posixpath.normpath(v) for v in values}
    return next(iter(distinct)) if len(distinct) == 1 else None


def _shown_root(run: SessionRun) -> tuple[str, str] | None:
    """The session directory as the session shows it, and where: the one directory its commands
    were recorded running in, else what every bare `pwd` (then `git rev-parse --show-toplevel`)
    printed. Two different answers from one source give none."""
    commands = [e for e in run.events if isinstance(e, CommandEvent) and e.status is CallStatus.OK]
    if found := _single([c.cwd for c in commands if c.cwd and posixpath.isabs(c.cwd)]):
        return found, "the directory its commands ran in"
    for printer in _ROOT_PRINTERS:
        printed = [
            c.output.strip()
            for c in commands
            if c.cwd is None and c.command.strip() == printer and not c.output_truncated
        ]
        if (found := _single(printed)) and posixpath.isabs(found) and "\n" not in found:
            return found, f"the output of `{printer}`"
    return None


def place(run: SessionRun, cwd: str | None) -> SessionRun:
    """`run` with a session directory when its record names none: `cwd` (the spec's), else the
    one the session shows."""
    if run.cwd is not None:
        return run
    if cwd is not None:
        return run.model_copy(update={"cwd": cwd})
    if (shown := _shown_root(run)) is None:
        return run
    note = f"the session directory {shown[0]} is taken from {shown[1]}"
    return run.model_copy(update={"cwd": shown[0], "notes": [*run.notes, note]})


_ONE: Final = "?"
_SEGMENT: Final = "*"
_ANY: Final = "**"
_DIRS: Final = "**/"


@dataclass(frozen=True, slots=True)
class _Class:
    negated: bool
    members: str
    ranges: tuple[tuple[str, str], ...]

    def matches(self, char: str) -> bool:
        inside = char in self.members or any(lo <= char <= hi for lo, hi in self.ranges)
        return char != "/" and inside is not self.negated


def _class(pattern: str, start: int) -> tuple[_Class, int] | None:
    """The `[...]` or `[!...]` class at `start` and the index after it, or None when the
    bracket is never closed (then it is a literal `[`, as in fnmatch)."""
    i = start + 1
    negated = i < len(pattern) and pattern[i] == "!"
    i += negated
    first = i
    members: list[str] = []
    ranges: list[tuple[str, str]] = []
    while i < len(pattern) and (pattern[i] != "]" or i == first):
        if i + 2 < len(pattern) and pattern[i + 1] == "-" and pattern[i + 2] != "]":
            ranges.append((pattern[i], pattern[i + 2]))
            i += 3
        else:
            members.append(pattern[i])
            i += 1
    if i >= len(pattern):
        return None
    return _Class(negated, "".join(members), tuple(ranges)), i + 1


_Token = str | _Class


def _glob_tokens(pattern: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    i = 0
    while i < len(pattern):
        if pattern[i] == "[" and (found := _class(pattern, i)):
            tokens.append(found[0])
            i = found[1]
            continue
        token = next(
            (t for t in (_DIRS, _ANY, _SEGMENT, _ONE) if pattern.startswith(t, i)), pattern[i]
        )
        tokens.append(token)
        i += len(token)
    return tuple(tokens)


def _glob_fullmatch(tokens: tuple[_Token, ...], text: str) -> bool:
    """Whether `tokens` match all of `text`, by stepping every live position at once.

    Each character advances the whole set of pattern positions, so the time is the pattern
    length times the text length whatever the glob: a spec glob cannot make it backtrack.
    `**/` matches nothing or anything ending in '/', `**` anything, `*`, `?` and a `[...]`
    class anything within a path segment.
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
            if isinstance(token, _Class):
                if token.matches(char):
                    after.add(i + 1)
            elif token == _DIRS:
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


def project_path(path: str, root: str | None, here: str | None = None) -> str | None:
    """`path` relative to the session directory `root`, resolved from the directory `here` it
    was named in; None when it lies outside the project (an absolute path not under `root`,
    or a relative one that climbs out with `..`) or cannot be placed (`root` unknown)."""
    if root and here and not posixpath.isabs(path):
        path = posixpath.join(here, path)
    rel = relative(path, root)
    if posixpath.isabs(rel) or rel == ".." or rel.startswith("../"):
        return None
    return rel.removeprefix("./")


def path_matches(pattern: str, path: str, root: str | None = None, here: str | None = None) -> bool:
    """Gitignore-like: a pattern without '/' matches a file or directory name at any depth; a
    pattern with '/' (a leading one included) matches from the session directory, and a
    directory pattern matches everything under it. A path outside the project matches no
    pattern."""
    rel = project_path(path, root, here)
    if rel is None:
        return False
    pat = pattern.rstrip("/")
    anchored = pat.startswith("/")
    pat = pat.removeprefix("/")
    tokens = _glob_tokens(pat)
    if not anchored and "/" not in pat:
        return any(_glob_fullmatch(tokens, part) for part in rel.split("/") if part)
    return _glob_fullmatch(tokens, rel) or _glob_fullmatch(_glob_tokens(pat + "/**"), rel)


def cite(event: SessionEvent, excerpt: str | None = None) -> Citation:
    return Citation(evidence_id=event.id, excerpt=(excerpt or "")[:EXCERPT] or None)


def result(status: Support, reason: str, *events: SessionEvent) -> PredicateResult:
    citations = [
        cite(e, e.command if isinstance(e, CommandEvent) else getattr(e, "path", None))
        for e in {event.id: event for event in events}.values()
    ]
    return PredicateResult(status=status, reason=reason, citations=citations, checker=CHECKER)
