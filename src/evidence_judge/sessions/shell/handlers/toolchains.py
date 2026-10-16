"""Handlers for language toolchains (go, cargo, the JVM and .NET build tools, ...): their test,
build, lint and format subcommands."""

from __future__ import annotations

from typing import Final

from evidence_judge.sessions.models import Category
from evidence_judge.sessions.shell.classified import Classified
from evidence_judge.sessions.shell.env import (
    GO_REGEN,
    off,
)
from evidence_judge.sessions.shell.handlers.call import Call
from evidence_judge.sessions.shell.runners.cargo import cargo_filters
from evidence_judge.sessions.shell.runners.go import go_filters
from evidence_judge.sessions.shell.writes import (
    run_writes,
    tool_config_change,
)


def _go_test(c: Call) -> Classified:
    args = c.args[1:]
    filters, collect = go_filters(args)
    regen = next((a for a in args if GO_REGEN.fullmatch(a) and not off(a)), None)
    why = f"go test {regen} rewrites expected outputs" if regen else None
    written, _, after = run_writes("go test", args, c.cwd)
    wrote = f"go test {written[0][0]} writes {written[0][1]}" if written else None
    return Classified(
        Category.TEST,
        c.prog,
        "go test",
        filters,
        collect,
        mutation=why or wrote,
        opaque=why,
        touched=[path for _, path in written],
        writes_after=after,
    )


def _go(c: Call) -> Classified:
    prog, args, sub = c.prog, c.args, c.sub
    if sub == "test":
        return _go_test(c)
    if sub in ("build", "install"):
        return Classified(Category.BUILD, prog)
    if sub == "vet":
        return Classified(Category.LINT, prog)
    if sub in ("fmt", "generate", "get") or args[:2] == ["mod", "tidy"]:
        return Classified(Category.MUTATING, prog, mutation=f"go {sub} changes files")
    if why := tool_config_change(c.words):
        return Classified(Category.MUTATING, prog, mutation=why, touched=[])
    if sub in ("version", "env", "list", "doc", "help") or args[:2] == ["mod", "graph"]:
        return Classified(Category.READ_ONLY, prog)
    return Classified(Category.UNKNOWN, prog, mutation=f"go {sub} runs a program")


def _cargo_insta(c: Call) -> Classified | None:
    args = c.args
    action = args[1] if len(args) > 1 else ""
    if action in ("accept", "review", "reject") or (
        action == "test" and any(a in ("--accept", "--review") for a in args)
    ):
        why = f"cargo insta {action} rewrites expected outputs"
        return Classified(Category.MUTATING, c.prog, mutation=why, opaque=why)
    if action == "test":
        filters, collect = cargo_filters(args[2:])
        return Classified(Category.TEST, c.prog, "cargo insta test", filters, collect)
    return None


def _cargo(c: Call) -> Classified:
    prog, args, sub = c.prog, c.args, c.sub
    if sub in ("test", "t"):
        filters, collect = cargo_filters(args[1:])
        return Classified(Category.TEST, prog, "cargo test", filters, collect)
    if sub == "insta" and (insta := _cargo_insta(c)) is not None:
        return insta
    if sub == "nextest":
        filters, collect = cargo_filters(args[1:])
        return Classified(Category.TEST, prog, "cargo nextest", filters, collect)
    if sub in ("build", "b"):
        return Classified(Category.BUILD, prog)
    if sub in ("check", "c"):
        return Classified(Category.TYPECHECK, prog)
    if sub == "clippy":
        fix = "--fix" in args
        return Classified(
            Category.LINT, prog, mutation="clippy --fix rewrites files" if fix else None
        )
    if sub == "fmt":
        check = "--check" in args
        return Classified(
            Category.FORMAT, prog, mutation=None if check else "cargo fmt rewrites files"
        )
    if sub in ("tree", "metadata", "search", "version", "--version", "help", "doc"):
        return Classified(Category.READ_ONLY, prog)
    return Classified(Category.UNKNOWN, prog, mutation=f"cargo {sub} may change files")


def _jvm(c: Call) -> Classified:
    args = c.args
    goals = [a for a in args if not a.startswith("-")]
    filters = [
        a.partition("=")[0]
        for a in args
        if a.startswith(("-Dtest", "-pl", "--tests", "--projects"))
    ]
    if any(g in ("test", "verify", "check", "integration-test") for g in goals):
        return Classified(Category.TEST, c.prog, c.prog, filters)
    if any(g in ("build", "package", "compile", "install", "assemble") for g in goals):
        return Classified(Category.BUILD, c.prog)
    return Classified(Category.UNKNOWN, c.prog, mutation=f"{c.prog} runs a project task")


def _dotnet(c: Call) -> Classified:
    prog, args, sub = c.prog, c.args, c.sub
    if sub == "test":
        filters = [a for a in args[1:] if a in ("--filter",)] + [
            f"only {a}" for a in args[1:2] if not a.startswith("-")
        ]
        return Classified(Category.TEST, prog, "dotnet test", filters)
    if sub == "build":
        return Classified(Category.BUILD, prog)
    if sub == "format":
        return Classified(
            Category.FORMAT,
            prog,
            mutation=None if "--verify-no-changes" in args else "dotnet format rewrites files",
        )
    return Classified(Category.UNKNOWN, prog, mutation=f"dotnet {sub} may change files")


def _toolchain(c: Call) -> Classified:
    prog, args, sub = c.prog, c.args, c.sub
    if sub == "test":
        filters = [f"only {a}" for a in args[1:] if not a.startswith("-")]
        filters.extend(
            a for a in args[1:] if a.partition("=")[0] in ("--filter", "--only", "--exclude")
        )
        return Classified(Category.TEST, prog, f"{prog} test", filters)
    if sub in ("build", "compile"):
        return Classified(Category.BUILD, prog)
    if sub == "lint":
        return Classified(Category.LINT, prog)
    if sub == "check":
        return Classified(Category.TYPECHECK, prog)
    if sub in ("fmt", "format"):
        return Classified(
            Category.FORMAT,
            prog,
            mutation=None if "--check" in args else f"{prog} {sub} rewrites files",
        )
    return Classified(Category.UNKNOWN, prog, mutation=f"{prog} {sub} may change files")


TOOLCHAINS: Final = (
    (("go",), _go),
    (("cargo",), _cargo),
    (("mvn", "mvnw", "gradle", "gradlew"), _jvm),
    (("dotnet",), _dotnet),
    (("deno", "swift", "mix", "bazel", "bazelisk"), _toolchain),
)
