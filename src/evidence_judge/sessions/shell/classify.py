"""Classifying simple commands and analysing whole command lines."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from evidence_judge.sessions.models import Category, CommandAnalysis
from evidence_judge.sessions.shell.classified import Classified
from evidence_judge.sessions.shell.env import (
    command_env,
    exports,
    harmless_env,
    harness_change,
    is_harness_env,
    off,
    regenerates_env,
    run_env_allowed,
)
from evidence_judge.sessions.shell.guard_paths import (
    AGENT_CONFIG,
    JUDGE_NAME,
    SESSION_STORE,
    guard_tamper,
    writes_session_store,
)
from evidence_judge.sessions.shell.handlers.call import Call, Handler
from evidence_judge.sessions.shell.handlers.checkers import CHECKERS
from evidence_judge.sessions.shell.handlers.files import FILE_TOOLS, git_command, runs_commands
from evidence_judge.sessions.shell.handlers.packages import PACKAGE_MANAGERS
from evidence_judge.sessions.shell.handlers.testing import TEST_RUNNERS
from evidence_judge.sessions.shell.handlers.toolchains import TOOLCHAINS
from evidence_judge.sessions.shell.lexer import (
    MAX_NESTING,
    PYTHON,
    SHELLS,
    Pipeline,
    Simple,
    Wrapped,
    parse,
    program_name,
    strip_comments,
    tokenize,
    unspelled,
    unwrap,
)
from evidence_judge.sessions.shell.programs import (
    DETACHERS,
    PYTHON_SIDE_EFFECTS,
    READ_ONLY_PROGRAMS,
    VERSION_FLAGS,
)
from evidence_judge.sessions.shell.runners.lint import lint_scope
from evidence_judge.sessions.shell.runners.stop import stop_early
from evidence_judge.sessions.shell.writes import (
    written_code,
)

MAX_COMMAND_CHARS: Final = 100_000


_CHECKS: Final = frozenset({Category.BUILD, Category.LINT, Category.TYPECHECK, Category.FORMAT})


_AGENT_SUMMARY_TEXT: Final = re.compile(
    r"\b\d+\s+(?:passed|passing|failed|failing|failures?|errors?)\b|"
    r"\btest result:\s*(?:ok|FAILED)|\bTests?:?\s+\d+|\bOK\s*\(\d+\s+tests?|"
    r"\bRan\s+\d+\s+tests?|\bBUILD\s+SUCCESS|\ball\s+tests?\s+pass",
    re.IGNORECASE,
)


def _shell(c: Call) -> Classified | None:
    if c.depth >= MAX_NESTING:
        return None
    args = c.args
    script = next(
        (args[i + 1] for i, a in enumerate(args[:-1]) if re.fullmatch(r"-[a-z]*c[a-z]*", a)),
        None,
    )
    if script is None:
        return Classified(Category.UNKNOWN, c.prog, mutation=f"{c.prog} runs a script")
    nested = _analyse(script, c.cwd, c.depth + 1)
    if nested.runners:
        category = Category.TEST
    elif nested.mutation is not None:
        category = Category.MUTATING
    else:
        category = nested.categories[-1] if nested.categories else Category.READ_ONLY
    return Classified(category, c.prog, nested=nested)


def _register(*families: tuple[Iterable[str], Handler]) -> dict[str, Handler]:
    handlers: dict[str, Handler] = {}
    for programs, handler in families:
        for prog in programs:
            handlers.setdefault(prog, handler)
    return handlers


_HANDLERS: Final = _register(
    (SHELLS, _shell),
    *TEST_RUNNERS,
    *TOOLCHAINS,
    *PACKAGE_MANAGERS,
    *CHECKERS,
    (("git",), git_command),
    *FILE_TOOLS,
    (("xargs", "parallel", "watch", "eval", "source", "."), runs_commands),
)
"""The handler for each program it classifies; the first family to name a program owns it."""


def _fallback(c: Call) -> Classified:
    prog, args = c.prog, c.args
    if prog in READ_ONLY_PROGRAMS:
        return Classified(Category.READ_ONLY, prog)
    if "/" not in c.words[0] and len(args) == 1 and args[0] in VERSION_FLAGS:
        return Classified(Category.READ_ONLY, prog)
    if PYTHON.match(prog) and args[:1] == ["-c"] and len(args) >= 2:
        if PYTHON_SIDE_EFFECTS.search(args[1]) is None:
            return Classified(Category.READ_ONLY, prog)
        return Classified(Category.UNKNOWN, prog, mutation="python -c runs code that can write")
    return Classified(Category.UNKNOWN, prog, mutation=f"{prog} is not a known read-only program")


def _classify_program(
    words: list[str], env: dict[str, str], depth: int, cwd: str | None
) -> Classified:
    call = Call(words, program_name(words[0]), words[1:], env, depth, cwd)
    handler = _HANDLERS.get(call.prog)
    result = handler(call) if handler is not None else None
    if result is not None and result.runner is not None:
        result.stop_early = stop_early(result.runner, call.args)
    if result is not None and result.category in (Category.LINT, Category.TYPECHECK):
        result.check_filters, result.check_masked = lint_scope(call.prog, call.args)
    return result if result is not None else _fallback(call)


def _classify(simple: Simple, depth: int, cwd: str | None) -> Classified:
    wrapped = Wrapped()
    words = unwrap(list(simple.words), wrapped)
    if not words:
        result = Classified(Category.READ_ONLY, "")
    else:
        result = _classify_program(words, simple.env, depth, cwd)
    if result.runner is not None:
        result.filters.extend(wrapped.filters)
        result.relocations = [*wrapped.dirs, *result.relocations]
        if wrapped.filters or result.relocations:
            result.paths = None
    read_only = result.category is Category.READ_ONLY
    if simple.writes and result.mutation is None:
        result.mutation = f"writes {simple.writes[0]}"
        if result.category is Category.READ_ONLY:
            result.category = Category.MUTATING
    if simple.heredoc and result.category is Category.UNKNOWN:
        result.mutation = result.mutation or "feeds a heredoc to a program"
    if result.runner is not None:
        _runner_env(result, simple, cwd)
    named = result.touched
    if named is None:
        mutating = result.category is Category.MUTATING and not read_only
        named = [w for w in words[1:] if not w.startswith("-")] if mutating else []
    result.touched = [*named, *simple.writes]
    _opaque(result)
    return result


def _runner_env(result: Classified, simple: Simple, cwd: str | None) -> None:
    """What a test run's environment changes: harness variables it filters with, variables
    the run sees that the judge does not allow, and one that rewrites expected outputs."""
    changed = [
        name
        for name, value in simple.env.items()
        if is_harness_env(name) and name != "PYTEST_ADDOPTS" and not harmless_env(name, value, cwd)
    ]
    result.filters.extend(f"runs with {name} changed" for name in changed)
    if changed:
        result.paths = None
    run_env = command_env(simple)
    result.run_env = [
        *result.run_env,
        *(
            name
            for name, value in run_env.items()
            if not run_env_allowed(name) and not harmless_env(name, value, cwd)
        ),
    ]
    regen = next((n for n, v in run_env.items() if regenerates_env(n) and not off(f"={v}")), None)
    if regen is not None and result.mutation is None:
        result.mutation = result.opaque = f"{regen} set: the run rewrites expected outputs"


def _opaque(result: Classified) -> None:
    """Mark a write the judge cannot follow: by a program it does not know, or to a path the
    shell builds at run time."""
    if result.opaque is not None or result.mutation is None:
        return
    if result.category in (Category.UNKNOWN, Category.BUILD):
        result.opaque = result.mutation
    elif built := next((p for p in result.touched or [] if unspelled(p)), None):
        result.opaque = f"writes {built}, a path the shell builds at run time"


def _empty(reason: str, command: str) -> CommandAnalysis:
    return CommandAnalysis(
        categories=[Category.UNKNOWN],
        mutation=f"the command could not be parsed ({reason})",
        exit_masked_by="the command could not be parsed",
        parse_error=reason,
        opaque_write=f"the command could not be parsed ({reason})",
        session_store=SESSION_STORE.search(command) is not None,
        guard_tamper=(
            f"the command could not be parsed and names {m.group(0)}"
            if (m := AGENT_CONFIG.search(command) or JUDGE_NAME.search(command))
            else None
        ),
    )


def _cd_step(simple: Simple) -> str:
    """Where a `cd`, `pushd` or `popd` goes: its target as written, or `?` when only the shell
    knows (`popd`, `cd -`, `cd` alone, a variable)."""
    words = simple.words
    args = [w for w in words[1:] if not (w.startswith("-") and w != "-")]
    if program_name(words[0]) == "popd" or len(args) != 1:
        return "?"
    target = args[0]
    return "?" if target == "-" or target.startswith("~") or unspelled(target) else target


@dataclass(slots=True)
class _Walk:
    """What `_analyse` gathers from a command line's simple commands, in order."""

    agent_text: bool
    categories: list[Category] = field(default_factory=list[Category])
    programs: list[str] = field(default_factory=list[str])
    runners: list[str] = field(default_factory=list[str])
    filters: list[str] = field(default_factory=list[str])
    deleted: list[str] = field(default_factory=list[str])
    touched: list[str] = field(default_factory=list[str])
    written: list[str] = field(default_factory=list[str])
    cds: list[str] = field(default_factory=list[str])
    run_cds: list[str] = field(default_factory=list[str])
    run_env: list[str] = field(default_factory=list[str])
    persisting: list[str] = field(default_factory=list[str])
    config_files: list[str] = field(default_factory=list[str])
    mutating_at: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    test_paths: list[str] | None = None
    collect_only: bool = False
    mutation: str | None = None
    opaque: str | None = None
    lasting: str | None = None
    nested_store: bool = False
    nested_guard: str | None = None
    nested_after: bool = False
    run_after: bool = False
    primary: tuple[int, int] | None = None
    masked: str | None = None
    output_filter: str | None = None
    stop_early: str | None = None
    check_filters: list[str] = field(default_factory=list[str])
    check_masked: str | None = None


def _note(
    walk: _Walk, simple: Simple, classified: Classified, alone: bool, at: tuple[int, int]
) -> None:
    """Record what every simple command adds, whether or not it nests another command line."""
    walk.touched.extend(classified.touched or [])
    walk.opaque = walk.opaque or classified.opaque
    if classified.program in DETACHERS:
        walk.lasting = (
            walk.lasting or f"{classified.program} starts a job that outlives the command"
        )
    if classified.mutation is not None or (
        classified.nested is not None and classified.nested.mutates
    ):
        walk.mutating_at.append(at)
    walk.categories.append(classified.category)
    if classified.program:
        walk.programs.append(classified.program)
    if classified.program in ("cd", "pushd", "popd"):
        step = _cd_step(simple)
        alone = alone and not simple.subshell
        walk.cds.append(step if alone else "?")
        if alone:
            walk.persisting.append(step)


def _take_nested(
    walk: _Walk,
    simple: Simple,
    classified: Classified,
    nested: CommandAnalysis,
    at: tuple[int, int],
    cwd: str | None,
) -> None:
    """Fold in a command line run by `bash -c`, `xargs` and the like."""
    walk.runners.extend(nested.runners)
    walk.filters.extend(nested.test_filters)
    walk.stop_early = walk.stop_early or nested.stop_early
    walk.collect_only = walk.collect_only or nested.collect_only
    walk.deleted.extend(nested.deleted_paths)
    walk.agent_text = walk.agent_text or nested.agent_text
    walk.mutation = walk.mutation or nested.mutation
    walk.touched.extend(nested.touched_paths)
    walk.opaque = walk.opaque or nested.opaque_write
    walk.lasting = walk.lasting or nested.lasting_mutation
    walk.written.extend(nested.source_findings)
    walk.nested_store = walk.nested_store or nested.session_store
    walk.nested_guard = walk.nested_guard or nested.guard_tamper
    if nested.runners:
        walk.run_cds = [*walk.cds, *nested.run_cds]
        walk.test_paths = nested.test_paths
        outer = [
            name
            for name, value in command_env(simple).items()
            if not run_env_allowed(name) and not harmless_env(name, value, cwd)
        ]
        walk.run_env = [*nested.run_env, *outer]
        walk.config_files.extend(nested.config_files)
    walk.check_filters.extend(nested.check_filters)
    if classified.category in _CHECKS and not walk.runners and not nested.runners:
        walk.run_cds = [*walk.cds, *nested.run_cds]
    if nested.runners or (classified.category in _CHECKS and not walk.runners):
        walk.nested_after = nested.mutation_after_check
        walk.primary = at
        walk.masked = nested.exit_masked_by
        walk.output_filter = nested.output_filter


def _take(walk: _Walk, classified: Classified, at: tuple[int, int]) -> None:
    """Fold in a simple command that runs a program itself."""
    walk.mutation = walk.mutation or classified.mutation
    walk.deleted.extend(classified.deleted)
    if classified.runner is not None:
        walk.runners.append(classified.runner)
        walk.filters.extend(classified.filters)
        walk.stop_early = walk.stop_early or classified.stop_early
        walk.run_cds = [*walk.cds, *classified.relocations]
        walk.run_env = list(classified.run_env)
        walk.config_files.extend(classified.config_files)
        walk.run_after = classified.writes_after
        walk.test_paths = (
            classified.paths
            if classified.paths is not None
            else ([] if not classified.filters else None)
        )
        walk.collect_only = walk.collect_only or classified.collect_only
    elif classified.category not in _CHECKS or walk.runners:
        return
    else:
        walk.check_filters.extend(classified.check_filters)
        walk.check_masked = walk.check_masked or classified.check_masked
        walk.run_cds = [*walk.cds, *classified.relocations]
    walk.primary = at
    walk.masked = None
    walk.output_filter = None


def _first_filter(after_pipe: list[Simple]) -> str:
    """The program the check's output is piped into, skipping `tail`, which keeps the summary."""
    names = [program_name((unwrap(c.words) or ["?"])[0]) for c in after_pipe]
    return next((name for name in names if name != "tail"), names[0])


def _followed_by(pipelines: list[Pipeline], start: int) -> str | None:
    """Why the line's exit status is not the check's: a later pipeline joined by anything but
    `&&`."""
    for k in range(start, len(pipelines) - 1):
        op = pipelines[k].op_after
        if op != "&&":
            shown = " ".join(pipelines[k + 1].commands[0].words[:3])
            label = "a newline" if op == "\n" else repr(op)
            return f"it is followed by {label} and {shown!r}"
    return None


def _mask_exit(
    walk: _Walk, pipelines: list[Pipeline], pipefail: bool, depth: int, cwd: str | None
) -> None:
    """Why the check's exit status may not be the command line's, if it may not."""
    if walk.primary is None or walk.masked is not None:
        return
    p_index, c_index = walk.primary
    pipeline = pipelines[p_index]
    after_pipe = pipeline.commands[c_index + 1 :]
    if after_pipe:
        walk.output_filter = _first_filter(after_pipe)
        if not pipefail:
            walk.masked = f"its output is piped into {walk.output_filter}"
    if pipeline.commands[c_index].negated:
        walk.masked = "it is negated with !"
    if walk.masked is None and pipeline.op_after == "&":
        walk.masked = "it runs in the background"
    if walk.masked is None:
        walk.masked = _followed_by(pipelines, p_index)
    if walk.masked is None:
        trailing = [
            _classify(c, depth, cwd).category
            for later in pipelines[p_index + 1 :]
            for c in later.commands
        ]
        if any(cat is not Category.READ_ONLY for cat in trailing):
            walk.masked = "other commands run after it"


def _analyse(command: str, cwd: str | None, depth: int) -> CommandAnalysis:
    if len(command) > MAX_COMMAND_CHARS:
        return _empty(f"longer than {MAX_COMMAND_CHARS} characters", command)
    try:
        pipelines = parse(strip_comments(tokenize(command)))
    except ValueError as exc:
        return _empty(str(exc), command)

    walk = _Walk(agent_text=_AGENT_SUMMARY_TEXT.search(command) is not None)
    for p_index, pipeline in enumerate(pipelines):
        for c_index, simple in enumerate(pipeline.commands):
            classified = _classify(simple, depth, cwd)
            at = (p_index, c_index)
            _note(walk, simple, classified, len(pipeline.commands) == 1, at)
            if classified.nested is not None:
                _take_nested(walk, simple, classified, classified.nested, at, cwd)
            else:
                _take(walk, classified, at)

    if any(p.op_after in ("||", "&") for p in pipelines):
        walk.persisting = ["?" for _ in walk.persisting]
    background = any(p.op_after == "&" for p in pipelines)
    if walk.lasting is None and walk.mutation is not None and background:
        walk.lasting = f"{walk.mutation} in a background job"
    mutation_after = _mutation_after(walk)
    _mask_exit(walk, pipelines, re.search(r"\bpipefail\b", command) is not None, depth, cwd)
    return _analysis(walk, pipelines, command, cwd, mutation_after)


def _mutation_after(walk: _Walk) -> bool:
    """Whether the command line changes something after its check ran."""
    return walk.primary is not None and (
        any(position > walk.primary for position in walk.mutating_at)
        or (walk.nested_after and walk.primary in walk.mutating_at)
        or walk.run_after
    )


def _analysis(
    walk: _Walk, pipelines: list[Pipeline], command: str, cwd: str | None, mutation_after: bool
) -> CommandAnalysis:
    return CommandAnalysis(
        categories=walk.categories,
        programs=walk.programs,
        runners=walk.runners,
        test_filters=list(dict.fromkeys(walk.filters)),
        check_filters=list(
            dict.fromkeys(
                [
                    *walk.check_filters,
                    *([walk.check_masked] if walk.runners and walk.check_masked else []),
                ]
            )
        ),
        stop_early=walk.stop_early,
        collect_only=walk.collect_only,
        exit_masked_by=walk.masked or (None if walk.runners else walk.check_masked),
        output_filter=walk.output_filter,
        agent_text=walk.agent_text,
        mutation=walk.mutation,
        deleted_paths=walk.deleted,
        touched_paths=list(dict.fromkeys(walk.touched)),
        opaque_write=walk.opaque,
        lasting_mutation=walk.lasting,
        run_cds=walk.run_cds,
        run_env=walk.run_env,
        exports=exports(pipelines, cwd),
        config_files=list(dict.fromkeys(walk.config_files)),
        test_paths=walk.test_paths if len(walk.runners) == 1 else None,
        cd_targets=walk.persisting,
        mutation_after_check=mutation_after,
        session_store=walk.nested_store
        or writes_session_store(pipelines, command, walk.touched, walk.deleted, walk.opaque),
        harness_change=harness_change(pipelines, command, cwd),
        guard_tamper=walk.nested_guard
        or guard_tamper(pipelines, command, walk.touched, walk.deleted, walk.opaque),
        source_findings=list(dict.fromkeys([*walk.written, *written_code(pipelines, command)])),
    )


def analyse(command: str, cwd: str | None = None) -> CommandAnalysis:
    """Parse and classify a shell command line run in `cwd`."""
    return _analyse(command, cwd, 0)
