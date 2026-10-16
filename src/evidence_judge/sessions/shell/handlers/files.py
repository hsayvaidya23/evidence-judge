"""Handlers for git and the programs that edit, copy, fetch or remove files."""

from __future__ import annotations

import re
from typing import Final

from evidence_judge.sessions.models import Category
from evidence_judge.sessions.shell.classified import Classified
from evidence_judge.sessions.shell.handlers.call import Call
from evidence_judge.sessions.shell.programs import COPIERS, MUTATING, OPAQUE_WRITERS
from evidence_judge.sessions.shell.writes import (
    SCRATCH,
    git,
    maybe_directory,
    perl,
    sed,
)


def git_command(c: Call) -> Classified:
    return git(c.words)


def _sed(c: Call) -> Classified:
    return sed(c.args)


def _perl(c: Call) -> Classified:
    return perl(c.args)


def _awk(c: Call) -> Classified:
    program_text = " ".join(c.args)
    writes = "system(" in program_text or re.search(r"print[^|;]*>\s*\"", program_text) is not None
    return Classified(
        Category.MUTATING if writes else Category.READ_ONLY,
        c.prog,
        mutation="awk writes files or runs commands" if writes else None,
        touched=[],
        opaque="awk writes files or runs commands" if writes else None,
    )


_FIND_ACTIONS: Final = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
)


def _find(c: Call) -> Classified:
    acting = [a for a in c.args if a in _FIND_ACTIONS]
    return Classified(
        Category.MUTATING if acting else Category.READ_ONLY,
        c.prog,
        mutation=f"find {acting[0]} acts on files" if acting else None,
        touched=[],
        opaque=f"find {acting[0]} acts on the files it finds" if acting else None,
    )


def _curl(c: Call) -> Classified:
    args = c.args
    named = [
        args[i + 1] if a in ("-o", "--output") else a.partition("=")[2]
        for i, a in enumerate(args)
        if (a in ("-o", "--output") and i + 1 < len(args)) or a.startswith("--output=")
    ]
    remote = any(a in ("-O", "--remote-name", "--remote-name-all", "-J") for a in args)
    writes = bool(named) or remote
    return Classified(
        Category.MUTATING if writes else Category.READ_ONLY,
        c.prog,
        mutation="curl writes a file" if writes else None,
        touched=named,
        opaque="curl -O names the file after the URL" if remote else None,
    )


def _copier(c: Call) -> Classified:
    prog, args = c.prog, c.args
    named = [a for a in args if not a.startswith("-")]
    target = named[-1:] or [""]
    into_directory = (maybe_directory(target[0]) and not target[0].startswith(SCRATCH)) or any(
        a in ("-t", "--target-directory") or a.startswith("-t") for a in args
    )
    return Classified(
        Category.MUTATING,
        prog,
        mutation="mv moves files" if prog == "mv" else f"{prog} changes files",
        deleted=named[:-1] if prog == "mv" else [],
        touched=named if prog == "mv" else target,
        opaque=f"{prog} writes into a directory" if into_directory else None,
    )


def _file_writer(c: Call) -> Classified:
    prog = c.prog
    if prog in ("rm", "unlink", "shred"):
        deleted = [a for a in c.args if not a.startswith("-")]
        return Classified(
            Category.MUTATING, prog, mutation=f"{prog} deletes files", deleted=deleted
        )
    if prog in COPIERS:
        return _copier(c)
    if prog in OPAQUE_WRITERS:
        return Classified(
            Category.MUTATING,
            prog,
            mutation=f"{prog} changes files",
            opaque=f"{prog} writes files its command line does not name",
        )
    return Classified(Category.MUTATING, prog, mutation=f"{prog} changes files")


def runs_commands(c: Call) -> Classified:
    return Classified(Category.UNKNOWN, c.prog, mutation=f"{c.prog} runs other commands")


FILE_TOOLS: Final = (
    (("sed",), _sed),
    (("perl",), _perl),
    (("awk", "gawk"), _awk),
    (("find",), _find),
    (("curl",), _curl),
    ((*("rm", "unlink", "shred"), *COPIERS, *OPAQUE_WRITERS, *MUTATING), _file_writer),
)
