"""What a project wrapper (`make test`, `npm test`, `tox -e py`) runs, when the session shows it.

A wrapper's definition can pass the runner options its command line does not show: `pytest
-x`, `jest --bail`, a filter. The judge reads the command the wrapper ran from the wrapper's own
output (npm's `> jest` header, tox's `commands[0]> pytest`, make's echo of a recipe line) or
from a `package.json` or `Makefile` the session showed, and analyses it like a direct run. A
wrapper neither shows stays unseen, and its run cannot back a failure count or an inference
that a test is not among the failures.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from evidence_judge.ingest._json import as_object
from evidence_judge.sessions.models import Category, CommandAnalysis
from evidence_judge.sessions.shell.classify import analyse
from evidence_judge.sessions.shell.lexer import program_name
from evidence_judge.sessions.shell.runners.make import make_args

DEFINITION_FILES: Final = frozenset({"Makefile", "makefile", "GNUmakefile", "package.json"})
"""The files that define the wrappers the judge can read."""


_MAKE_FILES: Final = ("GNUmakefile", "makefile", "Makefile")
_MAKES: Final = frozenset({"make", "gmake"})
_PMS: Final = frozenset({"npm", "pnpm", "yarn", "bun"})
_PM_FLAGS: Final = frozenset({"--silent", "-s", "--if-present", "-w", "--workspaces"})
_CHECKS: Final = frozenset({Category.LINT, Category.TYPECHECK})
_MAX_LINES: Final = 400
_MAX_DEPTH: Final = 5

_NPM_HEADER: Final = re.compile(r"^> (?:\S*@\S* )?(?P<script>\S+)(?: \S.*)?$")
_TOX_ECHO: Final = re.compile(
    r"^\s*\S+:? (?:commands\[\d+\]> |run-test: commands\[\d+\] \| )(?P<command>.+)$"
)
_NOX_ECHO: Final = re.compile(r"^nox > (?P<command>.+)$")
_MAKE_NOISE: Final = re.compile(r"^g?make(?:\[\d+\])?: ")
_MAKE_IGNORED: Final = re.compile(r"^g?make(?:\[\d+\])?: \[[^\]]*\] Error \d+ \(ignored\)", re.M)
_MAKE_DIRECTIVE: Final = re.compile(
    r"^\s*(?:-?include|sinclude|ifeq|ifneq|ifdef|ifndef|define|export|unexport|override|vpath|"
    r"\.RECIPEPREFIX|\.ONESHELL|\.SHELLFLAGS|SHELL\s*:?=|MAKEFLAGS)\b"
)
_MAKE_ASSIGN: Final = re.compile(r"^([A-Za-z_][\w.-]*)\s*(\?|::?|\+|!)?=\s*(.*)$")
_MAKE_RULE: Final = re.compile(r"^([^:=#]+?)\s*(::?)(?!=)\s*([^=]*)$")
_MAKE_REF: Final = re.compile(r"\$(?:\(([^()]*)\)|\{([^{}]*)\}|(.))")


@dataclass(frozen=True, slots=True)
class _Step:
    command: str
    ignores_errors: bool = False


def _words(wrapper: str) -> list[str]:
    try:
        return shlex.split(wrapper)
    except ValueError:
        return []


def _script(words: list[str]) -> tuple[str, list[str]] | None:
    """The package script a `npm test`, `yarn run lint` or `pnpm test:unit` call runs, and the
    arguments it passes on."""
    rest = [w for w in words[1:] if w not in _PM_FLAGS]
    if not rest:
        return None
    if rest[0] in ("run", "run-script", "rum", "urn"):
        rest = rest[1:]
    elif rest[0] in ("t", "tst"):
        rest = ["test", *rest[1:]]
    if not rest:
        return None
    extra = rest[1:]
    return rest[0], extra[1:] if extra[:1] == ["--"] else extra


def _with_args(command: str, extra: list[str]) -> str:
    return f"{command} {shlex.join(extra)}" if extra else command


def _lines(output: str) -> list[str]:
    return [line.rstrip() for line in output.splitlines()[:_MAX_LINES]]


def _pm_echo(words: list[str], output: str) -> list[_Step] | None:
    """npm and pnpm print `> pkg@1.0.0 test` then `> jest` before the script runs; yarn 1 and
    bun print `$ jest`."""
    called = _script(words)
    if called is None:
        return None
    lines = _lines(output)
    for i, line in enumerate(lines[:-1]):
        header = _NPM_HEADER.match(line)
        if header and header["script"] == called[0] and lines[i + 1].startswith("> "):
            return [_Step(lines[i + 1][2:])]
    leading = [line for line in lines if line.strip()][:3]
    echo = next((line[2:] for line in leading if line.startswith("$ ")), None)
    return [_Step(echo)] if echo else None


def _make_echo(output: str) -> list[_Step] | None:
    """make echoes each recipe line before running it, unless the line starts with `@`. The
    recipe counts as shown only when the output starts with such an echo; a recipe that starts
    silently could print a decoy later."""
    lines = [
        line for line in _lines(output) if line.strip() and not _MAKE_NOISE.match(line.strip())
    ]
    if not lines or not _command_like(lines[0]):
        return None
    return [_Step(line) for line in lines if _command_like(line)]


def _command_like(line: str) -> bool:
    analysis = analyse(line)
    return bool(analysis.runners) or bool(_CHECKS & set(analysis.categories))


def echoed(wrapper: str, output: str) -> list[_Step] | None:
    """The commands the wrapper's own output says it ran, or None."""
    words = _words(wrapper)
    prog = program_name(words[0]) if words else ""
    if prog in _PMS:
        return _pm_echo(words, output)
    if prog in _MAKES:
        return _make_echo(output)
    pattern = {"tox": _TOX_ECHO, "nox": _NOX_ECHO}.get(prog)
    if pattern is None:
        return None
    steps = [_Step(m["command"]) for line in _lines(output) if (m := pattern.match(line))]
    return steps or None


def _package_script(text: str, words: list[str]) -> list[_Step] | None:
    called = _script(words)
    if called is None:
        return None
    try:
        data = as_object(json.loads(text))
    except (ValueError, TypeError):
        return None
    scripts = data.get("scripts")
    command = scripts.get(called[0]) if isinstance(scripts, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(command, str) or re.search(r"[$`]", command):
        return None
    return [_Step(_with_args(command, called[1]))]


@dataclass(slots=True)
class _Makefile:
    variables: dict[str, str]
    rules: dict[str, list[tuple[list[str], list[str]]]]


def _logical_lines(text: str) -> list[str]:
    return re.sub(r"[ \t]*\\\n[ \t]*", " ", text).splitlines()


def _parse_makefile(text: str) -> _Makefile | None:
    """Variables and rules of a Makefile in the plain subset the judge reads; None for one with
    conditionals, includes, exports, pattern or double-colon rules, or anything else."""
    variables: dict[str, str] = {"MAKE": "make"}
    rules: dict[str, list[tuple[list[str], list[str]]]] = {}
    recipe: list[str] | None = None
    for raw in _logical_lines(text):
        if raw.startswith("\t"):
            if recipe is None:
                return None
            recipe.append(raw[1:])
            continue
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if _MAKE_DIRECTIVE.match(line):
            return None
        if assign := _MAKE_ASSIGN.match(line):
            name, kind, value = assign.groups()
            if kind == "!":
                return None
            if kind == "?" and name in variables:
                pass
            elif kind == "+":
                variables[name] = f"{variables.get(name, '')} {value}".strip()
            else:
                variables[name] = value
            recipe = None
            continue
        rule = _MAKE_RULE.match(line)
        if rule is None or rule[2] == "::" or "%" in rule[1]:
            return None
        prereqs, _, inline = rule[3].partition(";")
        recipe = [inline.strip()] if inline.strip() else []
        for target in rule[1].split():
            rules.setdefault(target, []).append((prereqs.split(), recipe))
    return _Makefile(variables, rules)


def _expand(text: str, makefile: _Makefile, target: str, depth: int = 0) -> str | None:
    """`text` with its variable references expanded; None for one the judge cannot expand (a
    function call, an automatic variable other than `$@`, an undefined variable)."""
    if depth > _MAX_DEPTH:
        return None
    failed = False

    def one(match: re.Match[str]) -> str:
        nonlocal failed
        name = match[1] if match[1] is not None else match[2] if match[2] is not None else match[3]
        if name == "$":
            return "$"
        if name == "@":
            return target
        value = makefile.variables.get(name)
        expanded = _expand(value, makefile, target, depth + 1) if value is not None else None
        if expanded is None:
            failed = True
            return ""
        return expanded

    out = _MAKE_REF.sub(one, text)
    return None if failed else out


def _make_steps(
    makefile: _Makefile, target: str, seen: set[str], depth: int = 0
) -> list[_Step] | None:
    definitions = makefile.rules.get(target)
    if not definitions or depth > _MAX_DEPTH or target in seen:
        return None
    seen.add(target)
    recipes = [recipe for _, recipe in definitions if recipe]
    if len(recipes) > 1:
        return None
    steps: list[_Step] = []
    for prereqs, _ in definitions:
        for prereq in prereqs:
            if prereq in makefile.rules and prereq not in seen:
                inner = _make_steps(makefile, prereq, seen, depth + 1)
                if inner is None:
                    return None
                steps.extend(inner)
    for line in recipes[0] if recipes else []:
        prefix = re.match(r"^[@+\-\s]*", line)
        flags = prefix.group(0) if prefix else ""
        command = _expand(line[len(flags) :], makefile, target)
        if command is None:
            return None
        if command.strip():
            steps.append(_Step(command.strip(), "-" in flags))
    return steps


def _make_recipe(prog: str, words: list[str], files: Mapping[str, str]) -> list[_Step] | None:
    parsed = make_args(prog, words[1:])
    if parsed.files or parsed.dirs or parsed.assigns or parsed.unknown or not parsed.targets:
        return None
    text = next((files[name] for name in _MAKE_FILES if name in files), None)
    makefile = _parse_makefile(text) if text is not None else None
    if makefile is None:
        return None
    steps: list[_Step] = []
    for target in parsed.targets:
        found = _make_steps(makefile, target, set())
        if found is None:
            return None
        steps.extend(found)
    return steps


def defined(wrapper: str, files: Mapping[str, str]) -> list[_Step] | None:
    """The commands the wrapper's definition in `files` (by name, as the session last showed
    them) runs, or None."""
    words = _words(wrapper)
    prog = program_name(words[0]) if words else ""
    if prog in _PMS and "package.json" in files:
        return _package_script(files["package.json"], words)
    if prog in _MAKES:
        return _make_recipe(prog, words, files)
    return None


def _merge(analysis: CommandAnalysis, steps: list[_Step], cwd: str | None) -> CommandAnalysis:
    wrapper = analysis.wrapper or ""
    testing = bool(analysis.runners)
    runs = [(step, analyse(step.command, cwd)) for step in steps]
    relevant = [
        (step, run)
        for step, run in runs
        if (run.is_test if testing else bool(_CHECKS & set(run.categories)))
    ]
    if not relevant or any(run.wrapper_unseen for _, run in relevant):
        return analysis
    filters: list[str] = []
    stop = analysis.stop_early
    masked = analysis.exit_masked_by
    for step, run in relevant:
        shown = f"{wrapper} runs {step.command!r}"
        scope = run.test_filters if testing else run.check_filters
        filters.extend(f"{shown}: {why}" for why in scope)
        filters.extend(f"{shown} in {where}" for where in run.run_cds)
        filters.extend(f"{shown} with {name} set" for name in run.run_env)
        filters.extend(f"{shown} with configuration {path}" for path in run.config_files)
        if run.stop_early and stop is None:
            stop = f"{shown}: {run.stop_early}"
        if masked is None and step.ignores_errors:
            masked = f"{wrapper} ignores the errors of its recipe line {step.command!r}"
        if masked is None and run.exit_masked_by:
            masked = f"{shown}, whose exit status is not the runner's: {run.exit_masked_by}"
    key = "test_filters" if testing else "check_filters"
    return analysis.model_copy(
        update={
            "wrapper_unseen": None,
            "stop_early": stop,
            key: list(dict.fromkeys([*getattr(analysis, key), *filters])),
            "exit_masked_by": masked,
        }
    )


def resolve(
    analysis: CommandAnalysis, output: str, files: Mapping[str, str], cwd: str | None
) -> CommandAnalysis:
    """`analysis` of a wrapper run, with the options of the command the wrapper ran when its
    output or a definition the session showed names it (the definition first: make does not echo
    a recipe line marked `@`), and with make's `(ignored)` errors as a masked exit status."""
    wrapper = analysis.wrapper
    if wrapper is None:
        return analysis
    words = _words(wrapper)
    prog = program_name(words[0]) if words else ""
    if prog in _MAKES and analysis.exit_masked_by is None and _MAKE_IGNORED.search(output):
        analysis = analysis.model_copy(
            update={"exit_masked_by": f"{wrapper} ignored a failing recipe line"}
        )
    if analysis.wrapper_unseen is None:
        return analysis
    steps = defined(wrapper, files) or echoed(wrapper, output)
    return _merge(analysis, steps, cwd) if steps else analysis
