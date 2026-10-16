"""Handlers for test runners: which tests a run selects and what it rewrites."""

from __future__ import annotations

import shlex
from typing import Final

from evidence_judge.sessions.models import Category
from evidence_judge.sessions.shell.classified import Classified
from evidence_judge.sessions.shell.env import (
    pytest_regenerates,
)
from evidence_judge.sessions.shell.handlers.call import Call
from evidence_judge.sessions.shell.runners.js import js_filters
from evidence_judge.sessions.shell.runners.node import node_test_filters
from evidence_judge.sessions.shell.runners.pytest import pytest_filters
from evidence_judge.sessions.shell.runners.tox import tox_filters
from evidence_judge.sessions.shell.runners.unittest import unittest_filters
from evidence_judge.sessions.shell.writes import (
    run_writes,
)


def _pytest(c: Call) -> Classified:
    filters, collect, paths = pytest_filters(c.args, c.env)
    regen = next((a for a in c.args if pytest_regenerates(a)), None)
    written, emptied, after = run_writes("pytest", c.args, c.cwd)
    why = f"pytest {regen} rewrites expected outputs" if regen else None
    if written or emptied:
        option, path = (written or emptied)[0]
        why = why or f"pytest {option} writes {path}"
    return Classified(
        Category.TEST,
        c.prog,
        "pytest",
        filters,
        collect,
        paths=paths,
        mutation=why,
        opaque=f"pytest {regen} rewrites expected outputs" if regen else None,
        touched=[path for _, path in written],
        deleted=[path for _, path in emptied],
        writes_after=after,
    )


def _unittest(c: Call) -> Classified:
    filters, collect = unittest_filters(c.args)
    return Classified(Category.TEST, c.prog, "unittest", filters, collect)


def _js_runner(c: Call) -> Classified:
    prog, args = c.prog, c.args
    filters, collect = js_filters(prog, args)
    if prog == "vitest" and args[:1] in (["bench"], ["list"]):
        collect = True
    mutation = (
        "updates snapshots"
        if any(a in ("-u", "--updateSnapshot", "--update") for a in args)
        else None
    )
    return Classified(
        Category.TEST,
        prog,
        prog,
        filters,
        collect,
        mutation=mutation,
        opaque=f"{prog} -u rewrites the expected snapshots" if mutation else None,
    )


def _browser_runner(c: Call) -> Classified | None:
    sub = {"playwright": "test", "cypress": "run"}[c.prog]
    if c.args[:1] != [sub]:
        return None
    filters, collect = js_filters(c.prog, c.args[1:])
    return Classified(Category.TEST, c.prog, f"{c.prog} {sub}", filters, collect)


_SELECTING_OPTIONS: Final = frozenset(
    {"-e", "--example", "-t", "--tag", "--filter", "--group", "--testsuite", "-R", "-L", "-k",
     "--only-failures"}
)  # fmt: skip


def _plain_runner(c: Call) -> Classified:
    filters = [f"only {a}" for a in c.args if not a.startswith("-")]
    filters.extend(a.partition("=")[0] for a in c.args if a.partition("=")[0] in _SELECTING_OPTIONS)
    return Classified(Category.TEST, c.prog, c.prog, filters)


def _node(c: Call) -> Classified | None:
    if "--test" not in c.args:
        return None
    filters, update = node_test_filters(c.args)
    why = "node --test --test-update-snapshots rewrites the expected snapshots"
    return Classified(
        Category.TEST,
        c.prog,
        "node --test",
        filters,
        mutation=why if update else None,
        opaque=why if update else None,
    )


def _tox(c: Call) -> Classified:
    filters = tox_filters(c.prog, c.args)
    return Classified(Category.TEST, c.prog, c.prog, filters, wrapper=shlex.join(c.words))


TEST_RUNNERS: Final = (
    (("pytest", "py.test"), _pytest),
    (("unittest",), _unittest),
    (("jest", "vitest", "mocha", "ava", "tap", "jasmine"), _js_runner),
    (("playwright", "cypress"), _browser_runner),
    (("rspec", "phpunit", "pest", "ctest", "behave", "nosetests", "nose2", "ward"), _plain_runner),
    (("node", "nodejs"), _node),
    (("tox", "nox"), _tox),
)
