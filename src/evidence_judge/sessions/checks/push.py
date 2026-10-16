"""Checking the remote and branch an answer says it pushed to against the pushes the session ran.

A successful `git push` shows where it went in its arguments (`git push origin main`,
`git push -u origin HEAD:fix`) and in git's own ref lines (`   0f1e2d3..1a2b3c4  main -> main`).
A push the record shows going elsewhere contradicts the claim; one whose remote or branch
neither shows is no evidence for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from evidence_judge.core.verdict import PredicateResult, Support
from evidence_judge.sessions.checks.common import result
from evidence_judge.sessions.checks.index import SessionIndex
from evidence_judge.sessions.checks.runs import exit_reliable
from evidence_judge.sessions.models import CallStatus, CommandEvent
from evidence_judge.sessions.shell.lexer import (
    flags_then_rest,
    parse,
    program_name,
    strip_comments,
    tokenize,
    unwrap,
)

_GIT_GLOBAL_VALUES: Final = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_PUSH_VALUES: Final = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
_NOT_A_PUSH: Final = frozenset({"-n", "--dry-run", "-d", "--delete", "--mirror", "--prune"})
_REF_LINE: Final = re.compile(
    r"^\s*(?P<flag>[ +\-*=!t])?\s*(?P<what>[0-9a-f]+\.\.\.?[0-9a-f]+|\[[^\]]+\])\s+"
    r"(?P<src>\S+)\s+->\s+(?P<dst>\S+)",
    re.M,
)
_DEFAULT_REMOTE: Final = "origin"


@dataclass(frozen=True, slots=True)
class PushTarget:
    """Where the answer says it pushed: a remote, a branch, or both, and the words it used."""

    remote: str | None
    branch: str | None
    quote: str

    def describe(self) -> str:
        if self.remote and self.branch:
            return f"{self.remote}/{self.branch}"
        return self.remote or f"branch {self.branch}"


@dataclass(frozen=True, slots=True)
class _Push:
    remote: str | None
    """The remote the command names; None for the default one."""
    branches: list[str] | None
    """The branches it updated, from git's ref lines or its refspecs; None when unknown."""


def _branch(ref: str) -> str | None:
    ref = ref.lstrip("+").removeprefix("refs/heads/")
    return None if ref in ("HEAD", "@") or ref.startswith("refs/") or not ref else ref


def _push_args(command: str) -> list[tuple[list[str], bool]]:
    """The arguments after `push` of each `git push` on the command line, and whether the
    line's exit status may not be the push's (a pipe, `||`, a later command)."""
    try:
        pipelines = parse(strip_comments(tokenize(command)))
    except ValueError:
        return []
    found: list[tuple[list[str], bool]] = []
    for at, pipeline in enumerate(pipelines):
        for simple in pipeline.commands:
            words = unwrap(list(simple.words))
            if not words or program_name(words[0]) != "git":
                continue
            args = flags_then_rest(words[1:], _GIT_GLOBAL_VALUES)
            if args[:1] == ["push"]:
                later = [p.op_after for p in pipelines[at:-1]]
                masked = len(pipeline.commands) > 1 or any(op != "&&" for op in later)
                found.append((args[1:], masked or simple.negated))
    return found


def _parse_push(args: list[str], output: str) -> _Push | None:
    """Where one `git push` went; None for one that pushes nothing (`--dry-run`, `--delete`)."""
    positional: list[str] = []
    remote: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, value = arg.partition("=")
        if name in _NOT_A_PUSH:
            return None
        if name == "--repo":
            remote = value if eq else (args[i + 1] if i + 1 < len(args) else None)
        if arg.startswith("-") and arg != "-":
            i += 2 if name in _PUSH_VALUES and not eq else 1
            continue
        positional.append(arg)
        i += 1
    if positional:
        remote = positional[0]
    shown = [
        branch
        for m in _REF_LINE.finditer(output)
        if m["flag"] not in ("!", "-") and "rejected" not in m["what"]
        if (branch := _branch(m["dst"])) is not None
    ]
    if shown:
        return _Push(remote, shown)
    specs = [spec for spec in positional[1:] if not spec.startswith(":")]
    branches = [_branch(spec.rpartition(":")[2] or spec.partition(":")[0]) for spec in specs]
    known = [b for b in branches if b is not None]
    return _Push(remote, known if specs and len(known) == len(branches) else None)


def _remote_matches(claimed: str, push: _Push) -> bool | None:
    remote = push.remote or _DEFAULT_REMOTE
    if re.search(r"[:/\\]", remote):
        return None
    return remote.casefold() == claimed.casefold()


def _matches(target: PushTarget, push: _Push) -> bool | None:
    """True when the push went where the answer says, False when it went elsewhere, None
    when the record does not show."""
    checks: list[bool | None] = []
    if target.remote:
        checks.append(_remote_matches(target.remote, push))
    if target.branch:
        branches = push.branches
        checks.append(None if branches is None else target.branch in branches)
    if False in checks:
        return False
    return None if None in checks else True


def check_push_target(target: PushTarget, index: SessionIndex) -> PredicateResult | None:
    """Whether a successful `git push` in the session went to the remote and branch the answer
    names; None when the session ran no `git push` at all, which the claim that it pushed
    already decides."""
    attempts: list[tuple[CommandEvent, list[str], bool]] = [
        (event, args, masked)
        for event in index.commands
        if event.status is not CallStatus.DENIED
        for args, masked in _push_args(event.command)
    ]
    if not attempts:
        return None
    where = target.describe()
    unknown: CommandEvent | None = None
    elsewhere: CommandEvent | None = None
    for event, args, masked in attempts:
        if not exit_reliable(event) or event.exit_code != 0:
            continue
        push = _parse_push(args, event.output)
        if push is None:
            continue
        matched = _matches(target, push)
        if masked and matched and not _REF_LINE.search(event.output):
            matched = None
        if matched:
            return result(Support.SUPPORTED, f"{event.id} pushed to {where}", event)
        if matched is None:
            unknown = event
        else:
            elsewhere = event
    if unknown is not None:
        return result(
            Support.NO_EVIDENCE,
            f"{unknown.id} pushed, but neither its arguments nor its output show it went to "
            f"{where}",
            unknown,
        )
    if elsewhere is not None:
        return result(
            Support.CONTRADICTED, f"{elsewhere.id} pushed somewhere other than {where}", elsewhere
        )
    last = attempts[-1][0]
    return result(Support.NO_EVIDENCE, f"no git push to {where} succeeded", last)
