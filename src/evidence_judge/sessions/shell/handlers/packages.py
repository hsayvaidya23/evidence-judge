"""Handlers for package managers and task runners: the scripts and targets they run."""

from __future__ import annotations

from typing import Final

from evidence_judge.sessions.models import Category
from evidence_judge.sessions.shell.classified import Classified
from evidence_judge.sessions.shell.handlers.call import Call
from evidence_judge.sessions.shell.handlers.checkers import builder, package_build
from evidence_judge.sessions.shell.runners.generic import generic_filters
from evidence_judge.sessions.shell.runners.make import make_like
from evidence_judge.sessions.shell.runners.pm import pm_script
from evidence_judge.sessions.shell.writes import (
    PM,
)


def _pm(c: Call) -> Classified:
    return pm_script(c.prog, c.words)


def _make(c: Call) -> Classified:
    return make_like(c.prog, c.words)


def _hatch(c: Call) -> Classified | None:
    if c.args[:1] == ["test"]:
        return Classified(
            Category.TEST, c.prog, "hatch test", generic_filters("hatch test", c.args[1:])
        )
    return package_build(c)


def _uv(c: Call) -> Classified:
    prog, args, sub = c.prog, c.args, c.sub
    if sub == "build":
        return Classified(Category.BUILD, prog)
    if sub in ("sync", "add", "remove", "lock", "venv", "init", "self", "tool") or args[:2] in (
        ["pip", "install"],
        ["pip", "uninstall"],
        ["pip", "sync"],
    ):
        return Classified(Category.MUTATING, prog, mutation=f"uv {sub} changes the environment")
    if sub in ("pip", "tree", "version", "--version", "help", "python"):
        return Classified(Category.READ_ONLY, prog)
    return Classified(Category.UNKNOWN, prog, mutation=f"uv {sub} may change files")


def _pip(c: Call) -> Classified:
    if c.sub in ("install", "uninstall", "download", "wheel"):
        return Classified(
            Category.MUTATING, c.prog, mutation=f"pip {c.sub} changes the environment"
        )
    return Classified(Category.READ_ONLY, c.prog)


PACKAGE_MANAGERS: Final = (
    (PM, _pm),
    (("make", "gmake", "just", "task", "mage"), _make),
    (("hatch",), _hatch),
    (("uv",), _uv),
    (("build",), builder),
    (("poetry", "pdm", "flit"), package_build),
    (("pip", "pip3"), _pip),
)
