"""Environment a test run sees: allowed variables, regeneration switches and harness changes."""

from __future__ import annotations

import posixpath
import re
from typing import Final

from evidence_judge.sessions.shell.lexer import (
    ASSIGNMENT,
    PYTHON,
    Pipeline,
    Simple,
    parse,
    program_name,
    strip_comments,
    tokenize,
    unspelled,
    unwrap,
)
from evidence_judge.sessions.shell.writes import PM, tool_config_change

RUN_ENV_ALLOWED: Final = frozenset(
    {"PYTHONPATH", "NO_COLOR", "FORCE_COLOR", "TERM", "PYTHONDONTWRITEBYTECODE", "COLUMNS",
     "PYTHONUNBUFFERED", "PY_COLORS", "CLICOLOR", "CLICOLOR_FORCE", "LINES", "PYTHONHASHSEED",
     "PYTHONIOENCODING", "PYTHONUTF8", "LANG", "LANGUAGE"}
)  # fmt: skip
"""Variables a test command may set without changing which tests run or how they decide. Any
other variable makes the run unable to prove the suite passes."""


def run_env_allowed(name: str) -> bool:
    return name in RUN_ENV_ALLOWED or name.startswith("LC_")


def command_env(simple: Simple) -> dict[str, str]:
    """The variables a simple command sets for its program: leading assignments and those
    passed through `env`."""
    env = dict(simple.env)
    words = simple.words
    if words and program_name(words[0]) == "env":
        for word in words[1:]:
            if word.startswith("-"):
                continue
            if not ASSIGNMENT.match(word):
                break
            name, _, value = word.partition("=")
            env[name] = value
    return env


HARNESS_ENV: Final = frozenset(
    {"PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
     "NODE_OPTIONS", "NODE_PATH", "GOFLAGS", "RUSTFLAGS", "RUSTC_WRAPPER", "LD_PRELOAD",
     "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "VIRTUAL_ENV", "BASH_ENV",
     "ENV", "JEST_CONFIG", "VITEST_CONFIG", "PYTHONOPTIMIZE", "PYTHONWARNINGS", "PYTHONUSERBASE",
     "PYTHONNOUSERSITE", "PYTHONSAFEPATH", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
     "MAVEN_OPTS", "GRADLE_OPTS", "RUSTC", "CARGO_BUILD_RUSTC_WRAPPER", "GOTOOLCHAIN",
     "ZDOTDIR", "GOENV", "UV_ENV_FILE"}
)  # fmt: skip
"""Variables that change which program runs, or how a test runner behaves."""


_HARNESS_ENV_PREFIX: Final = re.compile(
    r"^(?:PYTEST_|_PYTEST|JEST_|VITEST_|MOCHA_|NODE_TEST|CARGO_TARGET_\w+_RUNNER$|NEXTEST_|"
    r"npm_config_|NPM_CONFIG_)"
)


_PYTEST_REGEN_FLAGS: Final = frozenset(
    {"--snapshot-update", "--force-regen", "--regen-all", "--update-goldens", "--accept",
     "--update-snapshots", "--update-golden"}
)  # fmt: skip


_INLINE_SNAPSHOT_WRITES: Final = frozenset({"fix", "create", "update", "review", "trim"})


GO_REGEN: Final = re.compile(r"--?(?:update|regen|golden|bless|accept|overwrite)[\w-]*(?:=.*)?")


_REGEN_ENV: Final = re.compile(
    r"UPDATE|REGEN|BLESS|ACCEPT|OVERWRITE|SNAPSHOT|GOLDEN|INSTA_|TRYBUILD", re.IGNORECASE
)


_OFF: Final = frozenset({"", "0", "false", "no", "never", "off", "n"})


def off(arg: str) -> bool:
    """`-update=false`, `INSTA_UPDATE=no`: the switch is spelled but turned off."""
    _, eq, value = arg.partition("=")
    return bool(eq) and value.strip("'\"").lower() in _OFF


def pytest_regenerates(arg: str) -> bool:
    """A pytest switch that writes the expected outputs instead of comparing against them
    (pytest-regressions, syrupy, inline-snapshot, pytest-goldie and similar plugins)."""
    name, eq, value = arg.partition("=")
    if name == "--inline-snapshot":
        return bool(eq) and not _INLINE_SNAPSHOT_WRITES.isdisjoint(value.split(","))
    return name in _PYTEST_REGEN_FLAGS and not off(arg)


def regenerates_env(name: str) -> bool:
    """A variable that, by the usual naming, tells a test run to rewrite its expected outputs
    (`UPDATE_GOLDEN`, `INSTA_UPDATE`, `TRYBUILD=overwrite`, `SNAPSHOT_UPDATE`)."""
    return _REGEN_ENV.search(name) is not None


def is_harness_env(name: str) -> bool:
    return name in HARNESS_ENV or _HARNESS_ENV_PREFIX.match(name) is not None


_PROJECT_DIR: Final = re.compile(r"^(?:\.|\./|\$PWD|\$\{PWD\}|\$\(pwd\)|`pwd`)?$")


_PROJECT_PREFIX: Final = re.compile(r"^(?:\$PWD|\$\{PWD\}|\$\(pwd\)|`pwd`)/")


_INHERITED_PATH: Final = re.compile(r"^\$\{?PYTHONPATH\}?$")


_SETTINGS_MODULE: Final = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def _inside_project(entry: str, cwd: str | None) -> bool:
    """A `PYTHONPATH` entry naming the session directory or a directory inside it (`src`,
    `$PWD/lib`, an absolute path under `cwd`)."""
    if _PROJECT_DIR.match(entry):
        return True
    entry = _PROJECT_PREFIX.sub("", entry)
    if entry.startswith("~") or unspelled(entry):
        return False
    norm = posixpath.normpath(entry)
    if posixpath.isabs(norm):
        if cwd is None or not posixpath.isabs(cwd):
            return False
        norm = posixpath.relpath(norm, posixpath.normpath(cwd))
    return norm != ".." and not norm.startswith("../")


def harmless_env(name: str, value: str, cwd: str | None = None) -> bool:
    """A variable that changes no test's selection or outcome: `PYTHONPATH` naming only
    directories inside the session directory (`.`, `src`, `$PWD/lib`, with the inherited
    `$PYTHONPATH` after them), which `python -m pytest` and a src layout import from anyway;
    `DJANGO_SETTINGS_MODULE` naming a module (the settings module is project code, and its
    edits are checked like any other); and `NODE_ENV=test`, which jest sets itself."""
    if name == "PYTHONPATH":
        entries = value.split(":")
        if len(entries) > 1 and _INHERITED_PATH.match(entries[-1]):
            entries = entries[:-1]
        return all(_inside_project(entry, cwd) for entry in entries)
    if name == "DJANGO_SETTINGS_MODULE":
        return _SETTINGS_MODULE.fullmatch(value) is not None
    return name == "NODE_ENV" and value == "test"


def _sources(simple: Simple) -> str | None:
    """Why a simple command sets variables the command line does not name, or None."""
    words = simple.words
    prog = program_name(words[0]) if words else ""
    if prog in ("source", ".") and words[1:]:
        return f"{prog} {words[1]} sets whatever variables the file holds"
    if prog == "eval":
        return "eval runs text built at run time"
    if prog == "set" and (
        "allexport" in words or any(re.fullmatch(r"-[a-z]*a[a-z]*", w) for w in words[1:])
    ):
        return "set -a exports every variable set afterwards"
    if prog in ("export", "declare", "typeset", "readonly") and any(
        unspelled(w.partition("=")[0]) for w in words[1:] if not w.startswith("-")
    ):
        return f"{prog} sets variables named at run time"
    return None


_FUNCTION_DEF: Final = re.compile(
    r"(?:^|[;&|\n(]\s*)(?:function\s+[\w.:-]+|[\w.:-]+\s*\(\s*\))\s*\{"
)


_RUNNER_PACKAGES: Final = frozenset(
    {"pytest", "jest", "vitest", "mocha", "nose2", "tox", "nox", "pytest-asyncio", "pytest-cov"}
)
"""Packages an honest session installs to run the tests at all; anything else it installs can
register a test-runner plugin (a `pytest11` entry point, a jest preset)."""


_LAUNCH_WRAPPERS: Final = frozenset(
    {"time", "timeout", "nice", "env", "sudo", "doas", "command", "exec", "nohup", "caffeinate"}
)


_PACKAGE_SOURCE: Final = re.compile(
    r"^(?:\.{1,2}(?:/|$)|/|~|file:|git\+|(?:git|hg|svn|bzr)[+:]|https?://|github:|link:|"
    r"workspace:|portal:)|\.(?:whl|tar\.gz|tgz|zip)$|\s@\s",
    re.IGNORECASE,
)


_PROJECT_ITSELF: Final = re.compile(r"^\.(?:/)?(?:\[[\w,.-]+\])?$")


_INSTALL_VALUE_FLAGS: Final = frozenset(
    {"-r", "--requirement", "-c", "--constraint", "-i", "--index-url", "--extra-index-url",
     "-f", "--find-links", "--target", "-t", "--prefix", "--root", "--python", "-p",
     "--group", "--optional", "--extra", "--tag", "--registry", "--save-prefix", "-w",
     "--workspace", "--filter", "--dev-dep"}
)  # fmt: skip


def _foreign_source(value: str) -> bool:
    """A package spec naming a path, an archive, a URL or a VCS repository, not a registry name
    (`@scope/name` is a registry name)."""
    return _PACKAGE_SOURCE.search(value) is not None or ("/" in value and not value.startswith("@"))


def _package_word(value: str, why: str) -> str | None:
    """Why installing `value` changes the test harness, or None for the project itself and a
    test runner from the registry."""
    if _PROJECT_ITSELF.match(value):
        return None
    if _foreign_source(value):
        return f"{why} {value}, a local or VCS package that can hook into the test runner"
    name = re.split(r"[\[<>=!~;@ ]", value, maxsplit=1)[0].casefold()
    if name in _RUNNER_PACKAGES:
        return None
    return f"{why} {value}, a package the session adds, which can register a test-runner plugin"


def _install_targets(args: list[str]) -> list[tuple[str, bool]]:
    """The packages an install names, each with whether it is editable (`-e path`)."""
    out: list[tuple[str, bool]] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-e", "--editable") and i + 1 < len(args):
            out.append((args[i + 1], True))
            i += 2
            continue
        if arg.startswith("--editable="):
            out.append((arg.partition("=")[2], True))
        elif arg in _INSTALL_VALUE_FLAGS:
            i += 1
        elif not arg.startswith("-"):
            out.append((arg, False))
        i += 1
    return out


def _package_change(words: list[str]) -> str | None:
    """Why a command adds code the test runner may load: `uv run --with ./plugin`, `uvx --from
    git+...`, or installing a package other than the project itself and a test runner."""
    start = 0
    while start < len(words) and (
        program_name(words[start]) in _LAUNCH_WRAPPERS
        or words[start].startswith("-")
        or ASSIGNMENT.match(words[start])
        or words[start].replace(".", "").isdigit()
    ):
        start += 1
    words = words[start:]
    if not words:
        return None
    prog, args = program_name(words[0]), words[1:]
    if PYTHON.match(prog) and args[:2] == ["-m", "pip"]:
        prog, args = "pip", args[2:]
    if prog in ("uv", "uvx", "pipx"):
        for i, arg in enumerate(args):
            flag, eq, inline = arg.partition("=")
            if flag not in ("--with", "--with-editable", "--with-requirements", "--from"):
                continue
            value = inline if eq else (args[i + 1] if i + 1 < len(args) else "")
            if flag in ("--with-editable", "--with-requirements"):
                return f"{prog} {flag} {value} adds packages that can hook into the test runner"
            if _foreign_source(value):
                return f"{prog} {flag} {value}, a local or VCS package that can hook into the test runner"
    if prog == "uv" and args[:2] == ["pip", "install"]:
        prog, args = "pip", args[1:]
    adds = {
        "pip": ("install",),
        "pip3": ("install",),
        "uv": ("add",),
        "poetry": ("add",),
        "pdm": ("add",),
        "rye": ("add",),
        "npm": ("install", "i", "add", "link", "ln"),
        "pnpm": ("add", "install", "i", "link"),
        "yarn": ("add", "link"),
        "bun": ("add", "install", "i", "link"),
    }.get(prog)
    if adds is None or not args or args[0] not in adds:
        return None
    verb = f"{prog} {args[0]}"
    targets = _install_targets(args[1:])
    if prog in PM and args[0] in ("link", "ln"):
        return f"{verb} links {targets[0][0] if targets else 'a local package'} into node_modules"
    for value, editable in targets:
        if editable and not _PROJECT_ITSELF.match(value):
            return f"{verb} -e {value}, a local package that can hook into the test runner"
        if why := _package_word(value, f"{verb} installs"):
            return why
    return None


INSTALLERS: Final = (
    r"\b(?:pip3?|uv|poetry|pdm|pipx|npm|pnpm|yarn|bun|cargo|go|gem|bundle|apt(?:-get)?|brew|"
    r"conda|mamba)\b[^|;&\n]*?\b(?:install|add|get|i)\b"
)
"""The start of a command that installs a package it names."""


_FROM_MANIFEST: Final = {
    "uv": ("sync",),
    "poetry": ("install", "sync"),
    "pdm": ("install", "sync"),
    "pipenv": ("install", "sync"),
    "rye": ("sync",),
    "npm": ("ci", "install", "i"),
    "pnpm": ("install", "i"),
    "yarn": ("install",),
    "bun": ("install", "i"),
    "bundle": ("install",),
    "composer": ("install",),
    "cargo": ("fetch", "build"),
}
"""Package managers whose subcommand installs what the project's manifest or lock file lists
when it names no package."""


def _from_manifest(words: list[str]) -> bool:
    words = unwrap(words)
    if not words:
        return False
    prog, args = program_name(words[0]), words[1:]
    if PYTHON.match(prog) and args[:2] == ["-m", "pip"]:
        prog, args = "pip", args[2:]
    if prog == "uv" and args[:1] == ["pip"]:
        prog, args = "pip", args[1:]
        if args[:1] == ["sync"]:
            return True
    if prog in ("pip", "pip3") and args[:1] == ["install"]:
        flags = args[1:]
        if any(f == "--requirement" or f.startswith(("--requirement=", "-r")) for f in flags):
            return True
        targets = _install_targets(flags)
        return bool(targets) and all(_PROJECT_ITSELF.match(value) for value, _ in targets)
    if prog in ("conda", "mamba"):
        return args[:2] in (["env", "create"], ["env", "update"]) or (
            args[:1] == ["install"] and any(a == "--file" or a.startswith("--file=") for a in args)
        )
    if prog == "go":
        return args[:2] == ["mod", "download"]
    if prog == "yarn" and not args:
        return True
    subs = _FROM_MANIFEST.get(prog)
    if subs is None or not args or args[0] not in subs:
        return False
    return prog not in PM | {"pipenv"} or not _install_targets(args[1:])


def installs_from_manifest(command: str) -> bool:
    """The command installs packages from a requirements, manifest or lock file it does not
    spell out (`pip install -r requirements.txt`, `uv sync`, `npm ci`, `poetry install`), so
    which packages it installs is not on its command line."""
    try:
        pipelines = parse(strip_comments(tokenize(command)))
    except ValueError:
        return False
    return any(_from_manifest(list(s.words)) for p in pipelines for s in p.commands)


def exports(pipelines: list[Pipeline], cwd: str | None) -> list[str]:
    """Variables the command exports to later commands (`export CI=true`), beyond ones that only
    change output formatting."""
    names: list[str] = []
    for pipeline in pipelines:
        for simple in pipeline.commands:
            words = simple.words
            prog = program_name(words[0]) if words else ""
            exporting = prog == "export" or (
                prog in ("declare", "typeset") and any(re.fullmatch(r"-\w*x\w*", w) for w in words)
            )
            if not exporting:
                continue
            for word in words[1:]:
                name, eq, value = word.partition("=")
                if word.startswith("-") or not ASSIGNMENT.match(f"{name}="):
                    continue
                if not run_env_allowed(name) and not (eq and harmless_env(name, value, cwd)):
                    names.append(name)
    return names


def harness_change(pipelines: list[Pipeline], command: str, cwd: str | None) -> str | None:
    """Why a command changes how later commands run (aliases, functions, PATH, packages the
    runner may load), or None."""
    if _FUNCTION_DEF.search(command):
        return "defines a shell function"
    for pipeline in pipelines:
        for simple in pipeline.commands:
            words = simple.words
            if why := _package_change(list(words)):
                return why
            prog = program_name(words[0]) if words else ""
            if prog in ("alias", "unalias") and words[1:]:
                return f"{prog} {words[1].partition('=')[0]}"
            if why := tool_config_change(unwrap(list(words))):
                return why
            if prog == "hash" and "-p" in words:
                return "hash -p remaps a command"
            if prog in ("export", "declare", "typeset", "readonly", "set"):
                assignments = (w.partition("=") for w in words[1:] if "=" in w)
                names = [n for n, _, v in assignments if not harmless_env(n, v, cwd)]
                if changed := [n for n in names if is_harness_env(n)]:
                    return f"{prog} {changed[0]}"
            if not words and (
                changed := [
                    n
                    for n, v in simple.env.items()
                    if is_harness_env(n) and not harmless_env(n, v, cwd)
                ]
            ):
                return f"sets {changed[0]}"
            if why := _sources(simple):
                return why
    return None
