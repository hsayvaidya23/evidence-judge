"""Package-manager scripts: `npm test`, `pnpm run lint`, ..."""

from __future__ import annotations

import re
import shlex
from typing import Final

from evidence_judge.sessions.models import Category
from evidence_judge.sessions.shell.classified import Classified
from evidence_judge.sessions.shell.runners.generic import generic_filters
from evidence_judge.sessions.shell.runners.js import js_filters
from evidence_judge.sessions.shell.writes import tool_config_change

_PM_INSTALL: Final = frozenset(
    {
        "install",
        "i",
        "ci",
        "add",
        "remove",
        "rm",
        "uninstall",
        "un",
        "update",
        "up",
        "upgrade",
        "link",
        "unlink",
        "rebuild",
        "dedupe",
        "prune",
        "patch",
        "import",
        "create",
        "init",
    }
)


_PM_READ_ONLY: Final = frozenset(
    {
        "ls",
        "list",
        "view",
        "info",
        "outdated",
        "audit",
        "why",
        "explain",
        "config",
        "help",
        "version",
        "--version",
        "-v",
        "root",
        "bin",
        "prefix",
        "whoami",
        "search",
        "doctor",
    }
)


_TYPECHECK_SCRIPTS: Final = re.compile(r"^(?:type-?check|types|tsc|check-types|typecheck:.*)$")


_LINT_SCRIPTS: Final = re.compile(r"^lint(?::.*)?$")


_FORMAT_SCRIPTS: Final = re.compile(r"^(?:format|fmt|prettier)(?::.*)?$")


_BUILD_SCRIPTS: Final = re.compile(r"^(?:build|compile)(?::.*)?$")


FIX_FLAGS: Final = frozenset(
    {
        "--fix",
        "--fix-only",
        "--unsafe-fixes",
        "-a",
        "-A",
        "--autocorrect",
        "--autocorrect-all",
        "--write",
        "--apply",
        "--apply-unsafe",
        "-u",
        "--updateSnapshot",
        "--update-snapshots",
        "--snapshot-update",
        "--update",
    }
)


def pm_script(pm: str, words: list[str]) -> Classified:
    rest = [
        w for w in words[1:] if w not in ("--silent", "-s", "--if-present", "-w", "--workspaces")
    ]
    if not rest:
        return Classified(Category.MUTATING, pm, mutation=f"{pm} with no arguments installs")
    sub = rest[0]
    args = rest[1:]
    if sub in ("run", "run-script", "rum", "urn"):
        if not args:
            return Classified(Category.READ_ONLY, pm)
        sub, args = args[0], args[1:]
    elif sub in ("t", "tst"):
        sub = "test"
    elif sub in _PM_INSTALL:
        return Classified(Category.MUTATING, pm, mutation=f"{pm} {sub} changes dependencies")
    elif why := tool_config_change(words):
        return Classified(Category.MUTATING, pm, mutation=why, touched=[])
    elif sub in _PM_READ_ONLY:
        return Classified(Category.READ_ONLY, pm)
    elif pm == "bun" and sub == "test":
        filters, collect = js_filters("bun", args)
        return Classified(Category.TEST, pm, "bun test", filters, collect)
    label = f"{pm} {sub}" if sub == "test" else f"{pm} run {sub}"
    if sub == "test" or sub.startswith("test:"):
        filters = generic_filters(label, args)
        if sub != "test":
            filters.insert(0, f"script {sub} (not the project's test script)")
        return Classified(Category.TEST, pm, f"{pm} test", filters, wrapper=shlex.join(words))
    if _BUILD_SCRIPTS.match(sub):
        return Classified(Category.BUILD, pm)
    if _LINT_SCRIPTS.match(sub):
        fix = any(a in FIX_FLAGS for a in args) or "fix" in sub
        return Classified(
            Category.LINT,
            pm,
            mutation=f"{label} --fix rewrites files" if fix else None,
            wrapper=shlex.join(words),
        )
    if _TYPECHECK_SCRIPTS.match(sub):
        return Classified(Category.TYPECHECK, pm, wrapper=shlex.join(words))
    if _FORMAT_SCRIPTS.match(sub):
        checking = "check" in sub or any("check" in a for a in args)
        return Classified(
            Category.FORMAT, pm, mutation=None if checking else f"{label} rewrites files"
        )
    return Classified(Category.UNKNOWN, pm, mutation=f"{label} runs a project script")
