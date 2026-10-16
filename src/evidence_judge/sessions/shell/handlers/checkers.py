"""Handlers for linters, formatters, type checkers and build tools."""

from __future__ import annotations

from typing import Final

from evidence_judge.sessions.models import Category
from evidence_judge.sessions.shell.classified import Classified
from evidence_judge.sessions.shell.handlers.call import Call
from evidence_judge.sessions.shell.programs import BUILDERS, LINTERS, TYPECHECKERS
from evidence_judge.sessions.shell.runners.pm import FIX_FLAGS


def package_build(c: Call) -> Classified | None:
    return Classified(Category.BUILD, c.prog) if c.args[:1] == ["build"] else None


def _ruff(c: Call) -> Classified:
    args = c.args
    if c.sub == "format":
        check = any(a in ("--check", "--diff") for a in args)
        return Classified(
            Category.FORMAT, c.prog, mutation=None if check else "ruff format rewrites files"
        )
    fix = any(
        a.partition("=")[0] in ("--fix", "--unsafe-fixes", "--fix-only")
        and not a.endswith("=false")
        for a in args
    )
    return Classified(Category.LINT, c.prog, mutation="ruff --fix rewrites files" if fix else None)


def _python_formatter(c: Call) -> Classified:
    prog, args = c.prog, c.args
    check = any(a in ("--check", "--diff", "--check-only", "-c") for a in args)
    if prog in ("autopep8", "yapf", "autoflake"):
        check = not any(a in ("-i", "--in-place") for a in args)
    return Classified(Category.FORMAT, prog, mutation=None if check else f"{prog} rewrites files")


def _web_formatter(c: Call) -> Classified:
    prog, args = c.prog, c.args
    writes = any(
        a in ("--write", "-w", "--apply", "--apply-unsafe", "--fix") for a in args
    ) or args[:1] == ["fmt"]
    category = (
        Category.LINT
        if prog == "biome" and args[:1] in (["lint"], ["check"], ["ci"])
        else Category.FORMAT
    )
    return Classified(category, prog, mutation=f"{prog} rewrites files" if writes else None)


def _native_formatter(c: Call) -> Classified:
    prog, args = c.prog, c.args
    writes = any(a in ("-w", "-i", "--write") for a in args) or (
        prog == "rustfmt" and "--check" not in args
    )
    if prog == "terraform":
        writes = args[:1] == ["fmt"] and "-check" not in args
    return Classified(Category.FORMAT, prog, mutation=f"{prog} rewrites files" if writes else None)


def _pre_commit(c: Call) -> Classified:
    return Classified(Category.LINT, c.prog, mutation="pre-commit hooks may rewrite files")


def _linter(c: Call) -> Classified:
    fix = any(a.partition("=")[0] in FIX_FLAGS for a in c.args)
    return Classified(
        Category.LINT, c.prog, mutation=f"{c.prog} fixes rewrite files" if fix else None
    )


def _typechecker(c: Call) -> Classified:
    return Classified(Category.TYPECHECK, c.prog)


def _tsc(c: Call) -> Classified:
    no_emit = "--noEmit" in c.args or "--noemit" in c.args
    return Classified(Category.TYPECHECK if no_emit else Category.BUILD, c.prog)


def builder(c: Call) -> Classified:
    return Classified(Category.BUILD, c.prog)


def _docker(c: Call) -> Classified | None:
    return package_build(c)


def _cmake(c: Call) -> Classified | None:
    return Classified(Category.BUILD, c.prog) if "--build" in c.args else None


CHECKERS: Final = (
    (("ruff",), _ruff),
    (("black", "isort", "autopep8", "yapf", "autoflake"), _python_formatter),
    (("prettier", "biome", "dprint"), _web_formatter),
    (("gofmt", "goimports", "rustfmt", "clang-format", "shfmt", "terraform"), _native_formatter),
    (("pre-commit",), _pre_commit),
    (LINTERS, _linter),
    (TYPECHECKERS, _typechecker),
    (("tsc",), _tsc),
    (("vite", "next", "nuxt", "astro", "svelte-kit", "ng"), package_build),
    (BUILDERS, builder),
    (("docker",), _docker),
    (("cmake",), _cmake),
)
