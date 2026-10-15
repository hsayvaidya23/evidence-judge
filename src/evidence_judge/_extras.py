"""How to install an optional extra, for the errors that need one."""

from __future__ import annotations

REPOSITORY = "git+https://github.com/hsayvaidya23/evidence-judge"


def needs_extra(extra: str) -> str:
    return (
        f"this needs the {extra} extra: reinstall with it, e.g. `uv tool install --reinstall "
        f'"evidence-judge[{extra}] @ {REPOSITORY}"`, or `pip install "evidence-judge[{extra}]"` '
        "once it is on PyPI (see the install docs)"
    )
