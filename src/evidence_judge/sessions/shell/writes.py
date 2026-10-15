"""Files a command writes: redirections, git, sed, perl, tool configuration and written code."""

from __future__ import annotations

import ast
import posixpath
import re
from typing import Final

from evidence_judge.sessions import edits
from evidence_judge.sessions.models import Category
from evidence_judge.sessions.shell.classified import Classified
from evidence_judge.sessions.shell.lexer import (
    SAFE_TARGETS,
    Pipeline,
    flags_then_rest,
    program_name,
    unspelled,
    unwrap,
)

_GIT_MUTATING: Final = frozenset(
    {
        "apply",
        "checkout",
        "restore",
        "reset",
        "stash",
        "switch",
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "pull",
        "mv",
        "rm",
        "clean",
        "am",
        "worktree",
        "submodule",
        "clone",
        "init",
        "filter-branch",
        "filter-repo",
        "update-index",
        "read-tree",
        "checkout-index",
        "sparse-checkout",
        "lfs",
        "bisect",
    }
)


PM: Final = frozenset({"npm", "pnpm", "yarn", "bun"})


_WRITERS: Final = {
    "pytest": frozenset(
        {"--log-file", "--junitxml", "--junit-xml", "--html", "--benchmark-json",
         "--report-log", "--resultlog", "--result-log", "--json-report-file", "--debug",
         "--cov-report", "--basetemp", "--md-report-output"}
    ),
    "go test": frozenset(
        {"-coverprofile", "-cpuprofile", "-memprofile", "-blockprofile", "-mutexprofile",
         "-trace", "-o", "-outputdir"}
    ),
}  # fmt: skip
"""Options naming a file or directory a test run writes (a log, a report, a profile, the test
binary, pytest's temporary directory, which it empties first)."""


_WRITTEN_DIRS: Final = frozenset({"--basetemp", "-outputdir"})


def run_writes(
    runner: str, args: list[str], cwd: str | None
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], bool]:
    """(written, emptied, after): the (option, path) pairs a test run writes and empties that
    can matter, and whether it may leave code, configuration or tests changed after its own
    check. Paths under /tmp and outside the session directory do not matter, nor reports and
    profiles (`report.xml`, `cover.out`, `htmlcov/`), whose text the runner writes; a path
    among the tests, a configuration file or a runner's own file is the suite rewritten, and a
    code file (`--log-file=calc.py`) is a change after the check."""
    writers = _WRITERS[runner]
    written: list[tuple[str, str]] = []
    emptied: list[tuple[str, str]] = []
    after = False
    i = 0
    while i < len(args):
        name, eq, value = args[i].partition("=")
        if runner == "go test" and name.startswith("--"):
            name = name[1:]
        if name not in writers:
            i += 1
            continue
        if not eq:
            following = args[i + 1] if i + 1 < len(args) else ""
            optional = name == "--debug" and (not following or following.startswith("-"))
            value = "pytestdebug.log" if optional else following
            i += 0 if optional else 1
        i += 1
        directory = name in _WRITTEN_DIRS
        if name == "--cov-report":
            kind, colon, value = value.partition(":")
            if not colon or not value:
                continue
            directory = kind in ("html", "annotate")
        if not value or value in SAFE_TARGETS or value.startswith(SCRATCH):
            continue
        if _outside(value, cwd):
            continue
        weakens = (
            edits.is_test_path(value) or edits.is_harness_path(value) or edits.is_config_path(value)
        )
        if name == "--basetemp":
            emptied.append((name, value))
        if weakens or unspelled(value) or (not directory and edits.is_code_path(value)):
            written.append((name, value))
            after = after or not directory
    return written, emptied, after


def _outside(path: str, cwd: str | None) -> bool:
    """An absolute path outside the session directory `cwd`."""
    if not posixpath.isabs(path) or cwd is None or not posixpath.isabs(cwd):
        return False
    return posixpath.relpath(posixpath.normpath(path), posixpath.normpath(cwd)).startswith("..")


_CONFIG_WRITES: Final = frozenset({"set", "delete", "rm", "unset", "edit", "fix"})


def tool_config_change(words: list[str]) -> str | None:
    """Why the command changes settings a tool reads on every later run (`npm config set
    node-options=...`, `go env -w GOFLAGS=-run=X`), or None."""
    prog = program_name(words[0]) if words else ""
    args = [w for w in words[1:] if not w.startswith("-")]
    if prog in PM:
        verb = args[0] if args[:1] in (["set"], ["unset"]) else None
        if args[:1] in (["config"], ["c"]) and args[1:2] and args[1] in _CONFIG_WRITES:
            verb = args[1]
        if verb is not None:
            key = next((a for a in args[1:] if a not in _CONFIG_WRITES), "a setting")
            return f"{prog} config {verb} {key} changes how every later {prog} command runs"
    if prog == "go" and words[1:2] == ["env"]:
        flag = next((w for w in words[2:] if w in ("-w", "-u")), None)
        if flag is not None:
            key = next((a for a in args[1:]), "a variable")
            return f"go env {flag} {key} changes how every later go command runs"
    return None


def maybe_directory(path: str) -> bool:
    """A path that may name a directory (and so files inside it the command line does not
    name): a trailing slash, `.`, a glob, or a last component without an extension."""
    name = posixpath.basename(path.rstrip("/"))
    return path.endswith("/") or name in ("", ".", "..", "*") or "." not in name.lstrip(".")


def _git_paths(sub: str, rest: list[str]) -> list[str] | None:
    """The files `git checkout`/`restore` rewrite when its command line names them, else None
    (a branch switch, the whole tree, or a directory)."""
    if "--" in rest:
        paths = rest[rest.index("--") + 1 :]
    elif sub == "restore":
        paths = _positional(rest, frozenset({"-s", "--source"}))
    else:
        return None
    if not paths or any(maybe_directory(p) or unspelled(p) for p in paths):
        return None
    return paths


def _positional(args: list[str], value_flags: frozenset[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-") and arg != "-":
            if "=" not in arg and arg in value_flags:
                i += 1
        else:
            out.append(arg)
        i += 1
    return out


def git(words: list[str]) -> Classified:
    args = flags_then_rest(words[1:], frozenset({"-C", "-c", "--git-dir", "--work-tree"}))
    sub = args[0] if args else ""
    rest = args[1:]
    if sub not in _GIT_MUTATING:
        return Classified(Category.READ_ONLY, "git")
    if sub == "stash" and rest[:1] in (["list"], ["show"]):
        return Classified(Category.READ_ONLY, "git")
    if sub == "worktree" and rest[:1] == ["list"]:
        return Classified(Category.READ_ONLY, "git")
    mutation = f"git {sub} changes the working tree"
    named = _positional(rest, frozenset({"-m", "-s", "--source"}))
    if sub == "rm":
        return Classified(Category.MUTATING, "git", mutation=mutation, deleted=named, touched=[])
    if sub == "mv":
        return Classified(
            Category.MUTATING, "git", mutation=mutation, deleted=named[:-1], touched=named[-1:]
        )
    if sub in ("init", "update-index") or (
        sub == "reset" and not {"--hard", "--merge", "--keep"} & set(rest)
    ):
        return Classified(Category.MUTATING, "git", mutation=mutation, touched=[])
    new_branch = {"checkout": ("-b", "-B"), "switch": ("-c", "-C")}.get(sub, ())
    if rest[:1] and rest[0] in new_branch and len(named) == 1:
        return Classified(Category.MUTATING, "git", mutation=mutation, touched=[])
    if sub in ("checkout", "restore") and (paths := _git_paths(sub, rest)) is not None:
        return Classified(Category.MUTATING, "git", mutation=mutation, touched=paths)
    return Classified(
        Category.MUTATING,
        "git",
        mutation=mutation,
        touched=[],
        opaque=f"git {sub} can rewrite any file in the working tree",
    )


def sed(args: list[str]) -> Classified:
    in_place = False
    script_given = False
    files: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-e", "-f", "--expression", "--file", "-l", "--line-length"):
            script_given = script_given or arg not in ("-l", "--line-length")
            i += 2
            continue
        if arg.startswith(("--expression=", "--file=")):
            script_given = True
        elif arg.startswith("--in-place"):
            in_place = True
        elif arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            short = arg[1:]
            if "i" in short:
                in_place = True
                if short.endswith("i") and i + 1 < len(args) and args[i + 1] == "":
                    i += 1
            if short[-1:] in ("e", "f") and "i" not in short:
                script_given = True
                i += 1
        elif not script_given:
            script_given = True
        else:
            files.append(arg)
        i += 1
    if not in_place:
        return Classified(Category.READ_ONLY, "sed")
    return Classified(Category.MUTATING, "sed", mutation="sed -i edits files", touched=files)


def perl(args: list[str]) -> Classified:
    in_place = any(a.startswith("-") and not a.startswith("--") and "i" in a[1:] for a in args)
    scripts = [i for i, a in enumerate(args) if re.fullmatch(r"-[a-zA-Z0-9]*[eE]", a)]
    if not in_place or not scripts:
        return Classified(
            Category.MUTATING if in_place else Category.UNKNOWN,
            "perl",
            mutation="perl -i edits files" if in_place else "perl runs a program",
        )
    skip = {i + 1 for i in scripts}
    files = [a for i, a in enumerate(args) if i not in skip and not a.startswith("-")]
    return Classified(Category.MUTATING, "perl", mutation="perl -i edits files", touched=files)


SCRATCH: Final = ("/tmp/", "/private/tmp/", "/var/folders/", "/dev/")  # noqa: S108


_HEREDOC_BODY: Final = re.compile(
    r"(?<!<)<<(-?)[ \t]*(['\"]?)([\w.-]+)\2[^\n]*\n(.*?)(?:\n[ \t]*\3[ \t]*(?=\n|$)|\Z)", re.S
)


def _heredocs(command: str) -> list[str | None]:
    """Each heredoc's text, in order; None for one the shell expands (`$x`, backquotes)."""
    out: list[str | None] = []
    for m in _HEREDOC_BODY.finditer(command):
        body = m.group(4)
        if m.group(1):
            body = "\n".join(line.lstrip("\t") for line in body.split("\n"))
        expands = not m.group(2) and re.search(r"[$`\\]", body) is not None
        out.append(None if expands else body)
    return out


def _unescape(text: str) -> str:
    return text.replace("\\n", "\n").replace("\\t", "\t")


def _printed(words: list[str]) -> str | None:
    """What `echo` or `printf` prints, when the command line spells it out."""
    prog = program_name(words[0])
    args = words[1:]
    if prog == "echo":
        flags = [a for a in args if re.fullmatch(r"-[neE]+", a)]
        text = " ".join(args[len(flags) :]) if args[: len(flags)] == flags else None
        if text is not None and any("e" in f for f in flags):
            text = _unescape(text)
    elif prog == "printf" and len(args) == 1 and "%" not in args[0].replace("%%", ""):
        text = _unescape(args[0]).replace("%%", "%")
    else:
        return None
    return None if text is None or re.search(r"[$`]", text) else text


def _sed_replacements(args: list[str]) -> str | None:
    """The text `sed` substitutions insert, when every command is `s/pattern/text/flags`."""
    scripts: list[str] = []
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-e", "--expression"):
            scripts.extend(args[i + 1 : i + 2])
            i += 1
        elif arg.startswith("--expression="):
            scripts.append(arg.partition("=")[2])
        elif arg in ("-f", "--file") or arg.startswith("--file="):
            return None
        elif arg and not arg.startswith("-"):
            positional.append(arg)
        i += 1
    if not scripts:
        scripts = positional[:1]
    inserted: list[str] = []
    for script in scripts:
        for part in re.split(r"[;\n]", script):
            part = part.strip()
            if not part:
                continue
            if len(part) < 2 or part[0] != "s" or part[1].isalnum() or part[1] in "\\ ":
                return None
            pieces = re.split(rf"(?<!\\){re.escape(part[1])}", part[2:])
            if len(pieces) != 3 or not re.fullmatch(r"[gpiImMe0-9]*", pieces[2]):
                return None
            inserted.append(_unescape(pieces[1].replace("\\" + part[1], part[1])))
    return "\n".join(inserted)


def _code_findings(path: str, code: str | None) -> list[str]:
    name = posixpath.basename(path)
    if code is None:
        return [f"wrote {name} from the shell with code the judge cannot read"]
    try:
        ast.parse(code)
        chunks = [code]
    except (SyntaxError, ValueError):
        chunks = code.splitlines()
    return [
        finding
        for chunk in chunks
        for finding in edits.suite_findings(path, "modify", [], chunk.splitlines())
    ]


def written_code(pipelines: list[Pipeline], command: str) -> list[str]:
    """Findings on the code the command writes to Python files outside the tests: heredocs,
    `echo`/`printf` text and `sed` substitutions are checked like an edit's added lines; any
    other write to such a file is code the judge cannot read."""
    bodies = _heredocs(command)
    simples = [simple for pipeline in pipelines for simple in pipeline.commands]
    if sum(simple.heredoc for simple in simples) != len(bodies):
        bodies = [None] * len(simples)
    found: list[str] = []
    heredoc = 0
    for pipeline in pipelines:
        for index, simple in enumerate(pipeline.commands):
            body = bodies[heredoc] if simple.heredoc else None
            heredoc += simple.heredoc
            words = unwrap(list(simple.words))
            prog = program_name(words[0]) if words else ""
            targets = list(simple.writes)
            if prog == "tee":
                targets.extend(w for w in words[1:] if not w.startswith("-"))
            sed = prog == "sed" and any(re.fullmatch(r"-\w*i\S*|--in-place\S*", w) for w in words)
            if sed:
                targets.extend(words[2:])
            targets = [t for t in targets if t.endswith(".py") and not edits.is_test_path(t)]
            if not targets:
                continue
            feeder = unwrap(list(pipeline.commands[index - 1].words)) if index else []
            if simple.heredoc and (prog == "tee" or words in (["cat"], ["cat", "-"])):
                code = body
            elif sed:
                code = _sed_replacements(words[1:])
            elif prog in ("echo", "printf"):
                code = _printed(words)
            elif prog == "tee" and feeder and program_name(feeder[0]) in ("echo", "printf"):
                code = _printed(feeder)
            else:
                code = None
            for target in dict.fromkeys(targets):
                found.extend(_code_findings(target, code))
    return found
