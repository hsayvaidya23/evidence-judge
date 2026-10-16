"""What every session checker shares: the run outcome type, citations and results, and
glob matching of session paths."""

from __future__ import annotations

import posixpath
import re
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


def glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def path_matches(pattern: str, path: str) -> bool:
    """Gitignore-like: a pattern without '/' matches a file or directory name at any depth; a
    pattern with '/' matches from the session directory, and a directory pattern matches
    everything under it. Absolute paths outside the session directory match by suffix."""
    norm = posixpath.normpath(path).removeprefix("./")
    pat = pattern.rstrip("/")
    parts = [part for part in norm.split("/") if part]
    regex = glob_regex(pat)
    if "/" not in pat:
        return any(regex.fullmatch(part) for part in parts)
    under = glob_regex(pat + "/**")
    candidates = [norm]
    if norm.startswith(("/", "../")):
        candidates.extend("/".join(parts[i:]) for i in range(1, len(parts)))
    return any(regex.fullmatch(c) or under.fullmatch(c) for c in candidates)


def cite(event: SessionEvent, excerpt: str | None = None) -> Citation:
    return Citation(evidence_id=event.id, excerpt=(excerpt or "")[:EXCERPT] or None)


def result(status: Support, reason: str, *events: SessionEvent) -> PredicateResult:
    citations = [
        cite(e, e.command if isinstance(e, CommandEvent) else getattr(e, "path", None))
        for e in {event.id: event for event in events}.values()
    ]
    return PredicateResult(status=status, reason=reason, citations=citations, checker=CHECKER)
