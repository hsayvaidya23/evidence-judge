"""What a project wrapper (`make test`, `npm test`, `tox -e py`) runs, when the session shows it.

A wrapper's definition can pass the runner options its command line does not show: `pytest
-x`, `jest --bail`, a filter. The judge reads the command the wrapper ran from the wrapper's own
output (npm's `> jest` header, tox's `commands[0]> pytest`, make's echo of a recipe line) or
from a `package.json`, `Makefile`, justfile or `tox.ini` the session showed, with the variables
a Makefile exports or a tox configuration sets (in a tox.ini, setup.cfg or pyproject.toml), and
analyses it like a direct run. A spec's `trusted_wrappers` can declare what a wrapper runs
when the session shows neither. A wrapper none of these shows
stays unseen: its run cannot back a failure count or an inference that a test is not among the
failures, and its pass is the whole suite's only when it passed as many tests as an earlier
whole-suite run the session showed.
"""

from __future__ import annotations

import configparser
import json
import posixpath
import re
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from evidence_judge.ingest._json import as_list, as_object
from evidence_judge.sessions.edits.paths import (
    is_code_path,
    is_config_path,
    session_config_reason,
)
from evidence_judge.sessions.models import (
    CallStatus,
    Category,
    CommandAnalysis,
    CommandEvent,
    FileEditEvent,
    OtherToolEvent,
    SessionEvent,
    SubagentEvent,
)
from evidence_judge.sessions.shell.classify import analyse, unseen_reason
from evidence_judge.sessions.shell.lexer import program_name
from evidence_judge.sessions.shell.runners.make import make_args

DEFINITION_FILES: Final = frozenset(
    {"Makefile", "makefile", "GNUmakefile", "package.json", "justfile", "Justfile", ".justfile",
     "tox.ini", "setup.cfg", "pyproject.toml", ".npmrc"}
)  # fmt: skip
"""The files that define the wrappers the judge can read, or change what they print."""


_MAKE_FILES: Final = ("GNUmakefile", "makefile", "Makefile")
_JUST_FILES: Final = ("justfile", "Justfile", ".justfile")
_MAKES: Final = frozenset({"make", "gmake"})
_PMS: Final = frozenset({"npm", "pnpm", "yarn", "bun"})
_PM_FLAGS: Final = frozenset({"--silent", "-s", "--if-present", "-w", "--workspaces"})
_CHECKS: Final = frozenset({Category.LINT, Category.TYPECHECK})
_ENV_NAME: Final = re.compile(r"[A-Za-z_]\w*")
_PROSE: Final = re.compile(r"\.(?:md|rst|txt)$")
_DATA: Final = re.compile(r"\.(?:json|ya?ml|csv|tsv|xml)$|^\.git(?:ignore|attributes|keep)$")
"""Data and repository files, which make reads only through an `include` or a recipe that names
them."""
_ANY_READ: Final = "*"
_MAKE_READS_ANY: Final = re.compile(
    r"\$[({](?:shell|file|wildcard)\b|^[ \t]*(?:-?include|sinclude)\b.*\$", re.M
)
_MAKE_WORD: Final = re.compile(r"[\w./-]+")
_MAX_LINES: Final = 400
_MAX_DEPTH: Final = 5

_NPM_HEADER: Final = re.compile(r"^> (?:\S*@\S* )?(?P<script>\S+)(?: \S.*)?$")
_YARN_BANNER: Final = re.compile(r"^yarn run v1\.\d+\.\d+$")
_PM_NOISE: Final = re.compile(r"^npm (?:warn|WARN|notice|NOTICE)\b")
_SILENCING: Final[dict[str, frozenset[str]]] = {
    "npm": frozenset({"-s", "--silent", "-q", "--quiet", "--loglevel"}),
    "pnpm": frozenset({"-s", "--silent", "--reporter", "--loglevel"}),
    "yarn": frozenset({"-s", "--silent"}),
    "bun": frozenset({"--silent"}),
    "make": frozenset({"--silent", "--quiet"}),
    "gmake": frozenset({"--silent", "--quiet"}),
    "tox": frozenset({"-q", "-qq", "--quiet", "-p", "--parallel"}),
    "nox": frozenset({"-q", "--quiet"}),
}
"""Options that keep a wrapper from echoing what it runs (`tox -p` prints no command lines)."""
_MAKE_QUIET: Final = re.compile(
    r"^\t[\s+\-]*[@$]|\.SILENT|MAKEFLAGS|^\s*-?s?include\b|^[^\t#]*\$[({]", re.M
)
"""A Makefile whose recipe lines may run silently: a line starting with `@` or a variable
(`$(Q)pytest`), `.SILENT`, `MAKEFLAGS`, an include, or a variable reference outside a recipe
(a variable can hold an `@`)."""
_TOX_ECHO: Final = re.compile(
    r"^\s*\S+:? (?:commands\[\d+\]> |run-test: commands\[\d+\] \| )(?P<command>.+)$"
)
_NOX_ECHO: Final = re.compile(r"^nox > (?P<command>.+)$")
_MAKE_NOISE: Final = re.compile(r"^g?make(?:\[\d+\])?: ")
_MAKE_IGNORED: Final = re.compile(r"^g?make(?:\[\d+\])?: \[[^\]]*\] Error \d+ \(ignored\)", re.M)
_MAKE_DIRECTIVE: Final = re.compile(
    r"^\s*(?:-?include|sinclude|ifeq|ifneq|ifdef|ifndef|define|unexport|override|vpath)\b"
)
_MAKE_FUNCTION: Final = re.compile(r"\$[({][\w.-]+[ \t,]")
"""A function call (`$(shell ...)`, `$(eval ...)`, `$(call f,x)`): it can run commands or
define rules while make reads the file, whatever target runs."""
_MAKE_RESERVED: Final = frozenset(
    {"SHELL", "MAKESHELL", "MAKEFLAGS", "GNUMAKEFLAGS", "MFLAGS", "MAKE", "MAKEFILES",
     "MAKECMDGOALS", "MAKEOVERRIDES", "MAKELEVEL", "VPATH", "CURDIR", "MAKEFILE_LIST", "SUFFIXES"}
)  # fmt: skip
"""Variables that change how make runs a recipe (`SHELL = ./fake`) or what it reads."""
_MAKE_SPECIAL: Final = frozenset(
    {".PHONY", ".DELETE_ON_ERROR", ".PRECIOUS", ".SECONDARY", ".INTERMEDIATE", ".NOTPARALLEL",
     ".SUFFIXES"}
)  # fmt: skip
"""Special targets that change no recipe's command or exit status; any other (`.IGNORE`,
`.SILENT`, `.EXPORT_ALL_VARIABLES`, `.ONESHELL`, `.POSIX`, `.DEFAULT`) leaves a Makefile
unread."""
_MAKE_ASSIGN: Final = re.compile(r"^(export\s+)?([A-Za-z_][\w.-]*)\s*(\?|::?|\+|!)?=\s*(.*)$")
_SHELL_OPERATOR: Final = re.compile(r"[;&|`$()<>\n]")
_MAKE_RULE: Final = re.compile(r"^([^:=#]+?)\s*(::?)(?!=)\s*([^=]*)$")
_MAKE_REF: Final = re.compile(r"\$(?:\(([^()]*)\)|\{([^{}]*)\}|(.))")
_JUST_RECIPE: Final = re.compile(r"^@?([A-Za-z_][\w-]*)\s*:(?!=)\s*((?:[A-Za-z_][\w-]*\s*)*)$")
_TOX_ENV_OPTIONS: Final = frozenset({"-e", "--env"})
_TOX_POSARGS: Final = re.compile(r"\{posargs(?::([^{}]*))?\}")
_TOX_FACTOR: Final = re.compile(r"^[\w,!.-]+:\s")
_NPMRC_QUIET: Final = re.compile(r"^\s*(?:loglevel|silent|quiet|reporter)\s*=", re.M | re.I)
"""An `.npmrc` setting that may stop npm and pnpm from printing their `> pkg@1.0.0 test` header,
so a header in the output may be the script's own."""


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


def _silenced(prog: str, words: list[str]) -> bool:
    """The wrapper was told not to print its own echo (`npm --silent`, `make -s`, `tox -q`),
    so any echo in its output is the script's own."""
    for word in words[1:]:
        if word == "--":
            break
        name = word.partition("=")[0]
        if name in _SILENCING.get(prog, ()):
            return True
        if prog in _MAKES and re.fullmatch(r"-[A-Za-z]*s\w*", word):
            return True
    return False


def _pm_echo(words: list[str], output: str) -> list[_Step] | None:
    """npm and pnpm print `> pkg@1.0.0 test` then `> jest` before the script runs, yarn 1 its
    `yarn run v1.22.19` banner then `$ jest`, bun `$ jest`. The echo counts only when the output
    starts with the package manager's own banner and names the script once: a script, or a
    `pretest` before it, can print a decoy header of its own."""
    called = _script(words)
    lines = [line for line in _lines(output) if line.strip() and not _PM_NOISE.match(line)]
    if called is None or not lines:
        return None
    prog = program_name(words[0])
    if prog in ("npm", "pnpm"):
        if not _NPM_HEADER.match(lines[0]):
            return None
        found = [
            lines[i + 1][2:]
            for i, line in enumerate(lines[:-1])
            if (header := _NPM_HEADER.match(line))
            and header["script"] == called[0]
            and lines[i + 1].startswith("> ")
        ]
        return [_Step(found[0])] if len(found) == 1 else None
    if prog == "yarn":
        if not _YARN_BANNER.match(lines[0]):
            return None
        lines = lines[1:]
    echoes = [line for line in lines if line.startswith("$ ")]
    if not lines or not lines[0].startswith("$ ") or len(echoes) > 1:
        return None
    return [_Step(lines[0][2:])]


def _make_echo(output: str, files: Mapping[str, str]) -> list[_Step] | None:
    """make echoes each recipe line before running it, unless the line starts with `@`, so a
    recipe `@echo pytest; pytest -k x` prints what looks like an echo. The echo counts only
    when the session showed a Makefile (one the judge cannot read whole) none of whose recipe
    lines can be silent, and the output starts with it."""
    text = next((files[name] for name in _MAKE_FILES if name in files), None)
    if text is None or _MAKE_QUIET.search(text):
        return None
    lines = [
        line for line in _lines(output) if line.strip() and not _MAKE_NOISE.match(line.strip())
    ]
    if not lines or not _command_like(lines[0]):
        return None
    return [_Step(line) for line in lines if _command_like(line)]


def _command_like(line: str) -> bool:
    analysis = analyse(line)
    return bool(analysis.runners) or bool(_CHECKS & set(analysis.categories))


def echoed(wrapper: str, output: str, files: Mapping[str, str] | None = None) -> list[_Step] | None:
    """The commands the wrapper's own output says it ran, or None."""
    words = _words(wrapper)
    prog = program_name(words[0]) if words else ""
    if _silenced(prog, words):
        return None
    if prog in _PMS:
        return _pm_echo(words, output)
    if prog in _MAKES:
        return _make_echo(output, files or {})
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
    exported: list[str]


def _logical_lines(text: str) -> list[str]:
    return re.sub(r"[ \t]*\\\n[ \t]*", " ", text).splitlines()


def _parse_makefile(text: str) -> _Makefile | None:
    """Variables, rules and the variables it exports (`export PYTEST_ADDOPTS = -x`) of a
    Makefile in the plain subset the judge reads; None for one with conditionals, includes, a
    bare `export`, a function call, a `SHELL` or other make variable, a special target outside
    a few harmless ones, a variable in a target or prerequisite, pattern or double-colon rules,
    or anything else."""
    if _MAKE_FUNCTION.search(text):
        return None
    variables: dict[str, str] = {"MAKE": "make"}
    rules: dict[str, list[tuple[list[str], list[str]]]] = {}
    exported: list[str] = []
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
            export, name, kind, value = assign.groups()
            if kind == "!" or name in _MAKE_RESERVED or name.startswith("."):
                return None
            if kind in (":", "::"):
                value = _expand(value, _Makefile(variables, rules, exported), "")
                if value is None:
                    return None
                value = value.replace("$", "$$")
            if export:
                exported.append(name)
            if kind == "?" and name in variables:
                pass
            elif kind == "+":
                variables[name] = f"{variables.get(name, '')} {value}".strip()
            else:
                variables[name] = value
            recipe = None
            continue
        rule = _MAKE_RULE.match(line)
        if rule is None or rule[2] == "::" or "%" in rule[1] or "$" in rule[1]:
            return None
        if any(t.startswith(".") and t not in _MAKE_SPECIAL for t in rule[1].split()):
            return None
        if "$" in rule[3].partition(";")[0]:
            return None
        prereqs, _, inline = rule[3].partition(";")
        recipe = [inline.strip()] if inline.strip() else []
        for target in rule[1].split():
            rules.setdefault(target, []).append((prereqs.split(), recipe))
    return _Makefile(variables, rules, list(dict.fromkeys(exported)))


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
    env = _exported_env(makefile, target)
    if env is None:
        return None
    for line in recipes[0] if recipes else []:
        prefix = re.match(r"^[@+\-\s]*", line)
        flags = prefix.group(0) if prefix else ""
        command = _expand(line[len(flags) :], makefile, target)
        if command is None:
            return None
        if not command.strip():
            continue
        if env and _SHELL_OPERATOR.search(command):
            return None
        steps.append(_Step(f"{env}{command.strip()}", "-" in flags))
    return steps


def _exported_env(makefile: _Makefile, target: str) -> str | None:
    """The assignments a recipe line runs with for the variables the Makefile exports, as a
    command-line prefix (`PYTEST_ADDOPTS=-x `); None for a value the judge cannot expand."""
    parts: list[str] = []
    for name in makefile.exported:
        value = _expand(makefile.variables.get(name, ""), makefile, target)
        if value is None:
            return None
        parts.append(f"{name}={shlex.quote(value.strip())} ")
    return "".join(parts)


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


def _parse_justfile(text: str) -> dict[str, tuple[list[str], list[str]]] | None:
    """The recipes of a justfile, their dependencies and lines, in the plain subset the judge
    reads; None for one with settings, variables, parameters, attributes, imports, shebang
    recipes or `{{...}}` interpolation."""
    recipes: dict[str, tuple[list[str], list[str]]] = {}
    lines: list[str] | None = None
    for raw in _logical_lines(text):
        if not raw.strip():
            continue
        if raw[0] in " \t":
            body = raw.strip()
            if lines is None or "{{" in body or (not lines and body.startswith("#!")):
                return None
            if not body.startswith("#"):
                lines.append(body)
            continue
        if raw.startswith("#"):
            continue
        header = _JUST_RECIPE.match(raw.rstrip())
        if header is None or header[1] in recipes:
            return None
        lines = []
        recipes[header[1]] = (header[2].split(), lines)
    return recipes


def _just_steps(
    recipes: dict[str, tuple[list[str], list[str]]], name: str, seen: set[str]
) -> list[_Step] | None:
    if name not in recipes:
        return None
    if name in seen:
        return []
    seen.add(name)
    deps, lines = recipes[name]
    steps: list[_Step] = []
    for dep in deps:
        inner = _just_steps(recipes, dep, seen)
        if inner is None:
            return None
        steps.extend(inner)
    for line in lines:
        prefix = re.match(r"^[@\-]*", line)
        flags = prefix.group(0) if prefix else ""
        if command := line[len(flags) :].strip():
            steps.append(_Step(command, "-" in flags))
    return steps


def _just_recipe(words: list[str], files: Mapping[str, str]) -> list[_Step] | None:
    parsed = make_args("just", words[1:])
    if parsed.files or parsed.dirs or parsed.assigns or parsed.unknown:
        return None
    text = next((files[name] for name in _JUST_FILES if name in files), None)
    recipes = _parse_justfile(text) if text is not None else None
    if not recipes:
        return None
    seen: set[str] = set()
    steps: list[_Step] = []
    for target in parsed.targets or [next(iter(recipes))]:
        found = _just_steps(recipes, target, seen)
        if found is None:
            return None
        steps.extend(found)
    return steps


def _tox_config(text: str) -> configparser.ConfigParser | None:
    config = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        config.read_string(text)
    except configparser.Error:
        return None
    return config


def _tox_envs(words: list[str]) -> list[str]:
    envs: list[str] = []
    for i, word in enumerate(words):
        name, eq, value = word.partition("=")
        if name in _TOX_ENV_OPTIONS:
            value = value if eq else (words[i + 1] if i + 1 < len(words) else "")
        elif word.startswith("-e") and len(word) > 2:
            value = word[2:]
        else:
            continue
        envs.extend(e for part in value.split(",") for e in part.split() if e)
    return envs


def _tox_setenv(config: configparser.ConfigParser) -> list[str] | None:
    """Every variable a `setenv` or `set_env` of the config's test environments sets (a
    factor-conditional one too), as command-line prefixes; None for one the judge cannot read (a
    `file|` line)."""
    parts: list[str] = []
    for section in config.sections():
        if section != "testenv" and not section.startswith("testenv:"):
            continue
        text = "\n".join(config.get(section, key, fallback="") for key in ("setenv", "set_env"))
        for raw in text.splitlines():
            line = _TOX_FACTOR.sub("", raw.strip(), count=1)
            if not line or line.startswith("#"):
                continue
            name, eq, value = line.partition("=")
            if not eq or not _ENV_NAME.fullmatch(name.strip()):
                return None
            parts.append(_tox_assign(name.strip(), value.strip()))
    return parts


def _tox_assign(name: str, value: str) -> str:
    return f"{name}={shlex.quote(value.replace('{toxinidir}', '.'))} "


def _toml_setenv(table: object) -> list[str] | None:
    """The variables every `set_env` table of a native `[tool.tox]` sets; None for one the
    judge cannot read (a `file` entry, a substitution table)."""
    parts: list[str] = []
    items = list(as_object(table).items()) or [("", item) for item in as_list(table)]
    for key, value in items:
        if key in ("set_env", "setenv"):
            env = as_object(value)
            if env is not value:
                return None
            for name, text in env.items():
                if not isinstance(text, str) or name == "file" or not _ENV_NAME.fullmatch(name):
                    return None
                parts.append(_tox_assign(name, text))
            continue
        found = _toml_setenv(value)
        if found is None:
            return None
        parts.extend(found)
    return parts


def _pyproject_setenv(text: str) -> list[str] | None:
    """The variables the `[tool.tox]` of a pyproject.toml sets, in its `legacy_tox_ini` or as
    native tables; None for one the judge cannot read."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None if "tox" in text else []
    tox = as_object(as_object(data.get("tool")).get("tox"))
    legacy = tox.pop("legacy_tox_ini", None)
    found: list[str] | None = []
    if isinstance(legacy, str):
        config = _tox_config(legacy)
        found = _tox_setenv(config) if config is not None else None
    native = _toml_setenv(tox)
    return None if found is None or native is None else [*found, *native]


def _tox_env(files: Mapping[str, str]) -> str | None:
    """Every variable a tox configuration the session showed sets for its test environments (a
    tox.ini, the tox sections of a setup.cfg, the `[tool.tox]` of a pyproject.toml), as a
    command-line prefix; None for one the judge cannot read. Which of them tox reads depends on
    files the session may not show, so the variables of every one count."""
    parts: list[str] = []
    for name in ("tox.ini", "setup.cfg"):
        if name not in files:
            continue
        config = _tox_config(files[name])
        found = _tox_setenv(config) if config is not None else None
        if found is None and (name == "tox.ini" or "testenv" in files[name]):
            return None
        parts.extend(found or [])
    if "pyproject.toml" in files:
        found = _pyproject_setenv(files["pyproject.toml"])
        if found is None:
            return None
        parts.extend(found)
    return "".join(dict.fromkeys(parts))


def _tox_commands(config: configparser.ConfigParser, words: list[str]) -> list[_Step] | None:
    """The commands of the one environment `tox -e py` runs, from its section or `[testenv]`;
    None for several environments, or for a substitution or factor condition other than
    `{posargs}`."""
    envs = _tox_envs(words)
    if len(envs) != 1:
        return None
    section = f"testenv:{envs[0]}" if config.has_section(f"testenv:{envs[0]}") else "testenv"
    text = config.get(section, "commands", fallback=None)
    if text is None and section != "testenv":
        text = config.get("testenv", "commands", fallback=None)
    if not text:
        return None
    steps: list[_Step] = []
    for raw in _logical_lines(text):
        line = _TOX_POSARGS.sub(lambda m: m[1] or "", raw.strip())
        if not line or line.startswith("#"):
            continue
        if "{" in line or _TOX_FACTOR.match(line) or line.startswith("!"):
            return None
        ignores = line.startswith("-")
        steps.append(_Step(line.lstrip("- ").strip(), ignores))
    return steps or None


def _with_env(steps: list[_Step], env: str) -> list[_Step] | None:
    if not env:
        return steps
    if any(_SHELL_OPERATOR.search(step.command) for step in steps):
        return None
    return [_Step(f"{env}{step.command}", step.ignores_errors) for step in steps]


def defined(wrapper: str, files: Mapping[str, str]) -> list[_Step] | None:
    """The commands the wrapper's definition in `files` (by name, as the session last showed
    them) runs, or None."""
    words = _words(wrapper)
    prog = program_name(words[0]) if words else ""
    if prog in _PMS and "package.json" in files:
        return _package_script(files["package.json"], words)
    if prog in _MAKES:
        return _make_recipe(prog, words, files)
    if prog == "just":
        return _just_recipe(words, files)
    if prog == "tox" and "tox.ini" in files and (config := _tox_config(files["tox.ini"])):
        commands = _tox_commands(config, words)
        env = _tox_env(files)
        return _with_env(commands, env) if commands and env is not None else None
    return None


def _tox_echoed(words: list[str], output: str, files: Mapping[str, str]) -> list[_Step] | None:
    """What tox's output says it ran, with the variables a tox configuration the session
    showed sets."""
    steps = echoed(shlex.join(words), output)
    env = _tox_env(files)
    if steps is None or env is None:
        return None
    return _with_env(steps, env)


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
    last = max(i for i, (_, run) in enumerate(runs) if any(run is r for _, r in relevant))
    writing = [(step, run) for step, run in runs[: last + 1] if run.mutation or run.harness_change]
    if writing:
        return _writes(analysis, writing)
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


def _writes(
    analysis: CommandAnalysis, writing: list[tuple[_Step, CommandAnalysis]]
) -> CommandAnalysis:
    """`analysis`, still unseen, with what the recipe lines that may change files before the
    run write, delete or change (`rm tests/test_x.py`, `echo ... > conftest.py`), so the checks
    of a test-weakening command see them."""
    first = writing[0][1]
    runs = [run for _, run in writing]
    return analysis.model_copy(
        update={
            "deleted_paths": list(
                dict.fromkeys(
                    [*analysis.deleted_paths, *(p for r in runs for p in r.deleted_paths)]
                )
            ),
            "touched_paths": list(
                dict.fromkeys(
                    [*analysis.touched_paths, *(p for r in runs for p in r.touched_paths)]
                )
            ),
            "opaque_write": analysis.opaque_write
            or next((r.opaque_write for r in runs if r.opaque_write), None),
            "harness_change": analysis.harness_change
            or next((r.harness_change for r in runs if r.harness_change), None),
            "source_findings": list(
                dict.fromkeys(
                    [*analysis.source_findings, *(f for r in runs for f in r.source_findings)]
                )
            ),
            "suppressions": sorted(
                {*analysis.suppressions, *(x for r in runs for x in r.suppressions)}
            ),
            "wrapper_unseen": (
                f"{analysis.wrapper} runs {writing[0][0].command!r} before its check, which may "
                f"change what it checks ({first.mutation or first.harness_change})"
            ),
        }
    )


def resolve(
    analysis: CommandAnalysis, output: str, files: Mapping[str, str], cwd: str | None
) -> CommandAnalysis:
    """`analysis` of a wrapper run, with the options of the command the wrapper ran when its
    output or a definition the session showed names it (the definition first: make does not echo
    a recipe line marked `@`), and with make's `(ignored)` errors as a masked exit status. A
    package manager's header counts only when neither the run's variables nor an `.npmrc` the
    session showed may have silenced the real one."""
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
    if prog in _PMS and (
        any(n.casefold().startswith("npm_config_") for n in analysis.run_env)
        or _NPMRC_QUIET.search(files.get(".npmrc", ""))
    ):
        steps = defined(wrapper, files)
    elif prog == "tox":
        steps = defined(wrapper, files) or _tox_echoed(words, output, files)
    else:
        steps = defined(wrapper, files) or echoed(wrapper, output, files)
    if steps:
        return _merge(analysis, steps, cwd)
    if prog in _MAKES and (text := next((files[n] for n in _MAKE_FILES if n in files), None)):
        return analysis.model_copy(update={"wrapper_reads": _make_reads(text)})
    return analysis


def _make_reads(text: str) -> list[str]:
    """The names of the files a Makefile may include or name in a recipe: every path-like
    word; `*` when an include or a function picks the file at run time."""
    if _MAKE_READS_ANY.search(text):
        return [_ANY_READ]
    words = (w.rstrip(".") for w in _MAKE_WORD.findall(text) if "." in w.strip("."))
    return sorted({posixpath.basename(w) for w in words if w})


def wrapper_reads(events: list[SessionEvent]) -> frozenset[str]:
    """The names every Makefile the session showed but the judge cannot read may read."""
    return frozenset(
        name
        for event in events
        if isinstance(event, CommandEvent)
        for name in event.analysis.wrapper_reads
    )


def _redefines(path: str, reads: frozenset[str]) -> bool:
    """A change to `path` may change what a wrapper runs: a Makefile or a file make may include
    by its name (`*.mk`, `Makefile.local`), wrapper or tool configuration (`.env`, `tox.ini`,
    `.npmrc`), a file a Makefile the session showed may read, or a file the judge cannot place
    (`ci/rules`, `run.sh`). Source code and prose never do; data files (`config/app.yaml`,
    `.gitignore`) only when a shown Makefile may read them. A Makefile the session never showed
    could include a data file too; the judge cannot know, and trusts data files there."""
    name = posixpath.basename(path)
    if (
        name in DEFINITION_FILES
        or is_config_path(path)
        or session_config_reason(path) is not None
        or name in reads
    ):
        return True
    if is_code_path(name) or _PROSE.search(name):
        return False
    return _ANY_READ in reads or not _DATA.search(name)


def may_redefine(event: SessionEvent, reads: frozenset[str] = frozenset()) -> bool:
    """The event may have changed a wrapper's definition, its settings, how the shell runs it,
    or the directory it runs in; `reads` are the names shown Makefiles may read."""
    if isinstance(event, FileEditEvent):
        return event.status is not CallStatus.DENIED and _redefines(event.path, reads)
    if isinstance(event, SubagentEvent):
        return not event.transcript_loaded
    if isinstance(event, OtherToolEvent):
        return event.may_mutate
    if not isinstance(event, CommandEvent) or event.status is CallStatus.DENIED:
        return False
    analysis = event.analysis
    paths = [*analysis.touched_paths, *analysis.deleted_paths]
    return bool(
        analysis.opaque_write
        or analysis.lasting_mutation
        or analysis.parse_error
        or analysis.harness_change
        or analysis.cd_targets
        or (analysis.mutation and not paths)
        or any(_redefines(path, reads) for path in paths)
    )


def trust(
    events: list[SessionEvent], trusted: Mapping[str, str], cwd: str | None
) -> list[SessionEvent]:
    """`events`, with each run of a wrapper the spec's `trusted_wrappers` declares, which the
    session does not show, analysed as the declared command; not once the session may have
    changed what the wrapper runs, nor a run in another directory."""
    declared = {shlex.join(_words(k)): v for k, v in trusted.items()}
    reads = wrapper_reads(events)
    out: list[SessionEvent] = []
    changed = False
    for event in events:
        if (
            not changed
            and isinstance(event, CommandEvent)
            and (wrapper := event.analysis.wrapper) is not None
            and (command := declared.get(shlex.join(_words(wrapper)))) is not None
            and event.analysis.wrapper_unseen == unseen_reason(wrapper)
            and not event.analysis.run_cds
            and (event.cwd or cwd) == cwd
        ):
            analysis = _merge(event.analysis, [_Step(command)], event.cwd or cwd)
            event = event.model_copy(update={"analysis": analysis})
        changed = changed or may_redefine(event, reads)
        out.append(event)
    return out
