"""Canonical browser action names: the one mapping every ingester and checker shares."""

from __future__ import annotations

from typing import Final

from evidence_judge.textnorm import normalize

GOTO: Final = "goto"
CLICK: Final = "click"
FILL: Final = "fill"
PRESS: Final = "press"
SELECT: Final = "select"
CHECK: Final = "check"
UNCHECK: Final = "uncheck"
HOVER: Final = "hover"
UPLOAD: Final = "upload"
OTHER: Final = "other"

_SPELLINGS: Final[dict[str, tuple[str, ...]]] = {
    GOTO: ("goto", "navigate", "visit", "open", "go_to_url", "open_tab"),
    CLICK: (
        "click",
        "dblclick",
        "tap",
        "mouseClick",
        "touchscreenTap",
        "click_element",
        "click_element_by_index",
    ),
    FILL: (
        "fill",
        "type",
        "input",
        "input_text",
        "insert_text",
        "keyboardType",
        "keyboardInsertText",
    ),
    PRESS: ("press", "press_key", "keyboardPress", "send_keys"),
    SELECT: ("select", "selectOption", "select_dropdown", "select_dropdown_option"),
    CHECK: ("check",),
    UNCHECK: ("uncheck",),
    HOVER: ("hover",),
    UPLOAD: ("upload", "setInputFiles", "upload_file"),
}


def _key(name: str) -> str:
    return normalize(name).replace(" ", "_")


ACTION_NAMES: Final[dict[str, str]] = {
    _key(spelling): canonical
    for canonical, spellings in _SPELLINGS.items()
    for spelling in spellings
}
"""Every known spelling (Playwright API method, browser-use tool, predicate alias), keyed by
`normalize`d name, to its canonical action name."""


def known_action(name: str) -> str | None:
    """The canonical name of a known action spelling, else None."""
    return ACTION_NAMES.get(_key(name))


def canonical_action(name: str) -> str:
    """The canonical name of `name`; an unknown name is only normalised."""
    key = _key(name)
    return ACTION_NAMES.get(key, key)
