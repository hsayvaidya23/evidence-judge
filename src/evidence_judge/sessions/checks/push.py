"""Checking the remote and branch an answer says it pushed to against the pushes the session ran.

A successful `git push` shows where it went in its arguments (`git push origin main`,
`git push -u origin HEAD:fix`) and in git's own ref lines (`   0f1e2d3..1a2b3c4  main -> main`).
A push counts only when the line's exit status is its own and its output shows no rejected
ref or failed push, and ref lines are read only from git's report after its last `To <url>`
line, on a line that runs nothing after the push. A push the record shows going elsewhere, in
its refspecs or its report, contradicts the claim; one whose remote or branch neither shows is
no evidence for it.
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
_REPORT_START: Final = re.compile(r"^To \S.*$", re.M)
_FAILED: Final = re.compile(r"^\s*!\s*\[|^error: failed to push|^fatal:", re.M)


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
    specs: list[str] | None
    """The branches its refspecs name; None when they do not say."""
    shown: list[str]
    """The branches git's own report says it updated."""


@dataclass(frozen=True, slots=True)
class _Attempt:
    args: list[str]
    """The arguments after `push`."""
    status_known: bool
    """Whether the line's exit status is the push's: no pipe, `!` or `&`, and only `&&` after it."""
    report_trusted: bool
    """Whether git's report is the last output on the line: the push runs last, nothing beside
    it in the background."""


def _branch(ref: str) -> str | None:
    ref = ref.lstrip("+").removeprefix("refs/heads/")
    unnamed = ref in ("HEAD", "@") or ref.startswith("refs/") or "*" in ref
    return None if unnamed or not ref else ref


def _push_attempts(command: str) -> list[_Attempt]:
    """Each `git push` on the command line, and how far the line's status and output are its."""
    try:
        pipelines = parse(strip_comments(tokenize(command)))
    except ValueError:
        return []
    found: list[_Attempt] = []
    for at, pipeline in enumerate(pipelines):
        for simple in pipeline.commands:
            words = unwrap(list(simple.words))
            if not words or program_name(words[0]) != "git":
                continue
            args = flags_then_rest(words[1:], _GIT_GLOBAL_VALUES)
            if args[:1] != ["push"]:
                continue
            later = [p.op_after for p in pipelines[at:-1]]
            known = (
                len(pipeline.commands) == 1
                and not simple.negated
                and all(op == "&&" for op in later)
                and pipelines[-1].op_after != "&"
            )
            last = at == len(pipelines) - 1
            alone = all(p.op_after != "&" for p in pipelines[:at])
            found.append(_Attempt(args[1:], known, known and last and alone))
    return found


def _report(output: str) -> str:
    """Git's own report of a push: the text after its last `To <url>` line, which no pre-push
    hook or earlier command can print after."""
    starts = list(_REPORT_START.finditer(output))
    return output[starts[-1].end() :] if starts else ""


def _parse_push(attempt: _Attempt, output: str) -> _Push | None:
    """Where one `git push` went; None for one that pushes nothing (`--dry-run`, `--delete`)."""
    args = attempt.args
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
        for m in _REF_LINE.finditer(_report(output) if attempt.report_trusted else "")
        if m["flag"] not in ("!", "-") and "rejected" not in m["what"]
        if (branch := _branch(m["dst"])) is not None
    ]
    specs = [spec for spec in positional[1:] if not spec.startswith(":")]
    branches = [_branch(spec.rpartition(":")[2] or spec.partition(":")[0]) for spec in specs]
    known = [b for b in branches if b is not None]
    return _Push(remote, known if specs and len(known) == len(branches) else None, shown)


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
        for branches in (push.specs, push.shown):
            if branches:
                checks.append(target.branch in branches)
        if not push.specs and not push.shown:
            checks.append(None)
    if False in checks:
        return False
    return None if None in checks else True


def check_push_target(target: PushTarget, index: SessionIndex) -> PredicateResult | None:
    """Whether a successful `git push` in the session went to the remote and branch the answer
    names; None when the session ran no `git push` at all, which the claim that it pushed
    already decides."""
    attempts: list[tuple[CommandEvent, _Attempt]] = [
        (event, attempt)
        for event in index.commands
        if event.status is not CallStatus.DENIED
        for attempt in _push_attempts(event.command)
    ]
    if not attempts:
        return None
    where = target.describe()
    unknown: CommandEvent | None = None
    elsewhere: CommandEvent | None = None
    for event, attempt in attempts:
        if not attempt.status_known or not exit_reliable(event) or event.exit_code != 0:
            continue
        if _FAILED.search(event.output):
            continue
        push = _parse_push(attempt, event.output)
        if push is None:
            continue
        matched = _matches(target, push)
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
