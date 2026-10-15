"""How to install an optional extra, for the errors that need one."""

from __future__ import annotations

REPOSITORY = "git+https://github.com/hsayvaidya23/evidence-judge"


def needs_extra(extra: str) -> str:
    extras = ",".join(dict.fromkeys(["anthropic", "mcp", extra]))
    return (
        f"this needs the {extra} extra: reinstall with {extra} added to the extras you already "
        f'use, e.g. `uv tool install --python 3.12 --reinstall "evidence-judge[{extras}] @ '
        f'{REPOSITORY}"`, or `pip install "evidence-judge[{extra}] @ {REPOSITORY}"` '
        "(v0.1.0 is not released yet, so both install from GitHub; see the install docs)"
    )
