"""Canonical browser action names: the one mapping every ingester and checker shares."""

from __future__ import annotations

from typing import Final

from evidence_judge.textnorm import normalize

GOTO: Final = "goto"
CLICK: Final = "click"
FILL: Final = "fill"
PRESS: Final = "press"
KEY_DOWN: Final = "key_down"
"""A key pressed and held (``keyboard.down``): it types like PRESS, and a held modifier applies
to the keys that follow until its KEY_UP."""
KEY_UP: Final = "key_up"
SELECT: Final = "select"
CHECK: Final = "check"
UNCHECK: Final = "uncheck"
HOVER: Final = "hover"
UPLOAD: Final = "upload"
INJECT: Final = "inject"
"""The agent ran its own code in the page or replaced its content: what the page shows after
it is the agent's, not the site's."""
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
    KEY_DOWN: ("key_down", "keyboardDown"),
    KEY_UP: ("key_up", "keyboardUp"),
    SELECT: ("select", "selectOption", "select_dropdown", "select_dropdown_option"),
    CHECK: ("check",),
    UNCHECK: ("uncheck",),
    HOVER: ("hover",),
    UPLOAD: ("upload", "setInputFiles", "upload_file"),
    INJECT: (
        "inject",
        "evaluate",
        "evaluateHandle",
        "evaluateExpression",
        "evaluateExpressionHandle",
        "evalOnSelector",
        "evalOnSelectorAll",
        "waitForFunction",
        "addInitScript",
        "addScriptTag",
        "addStyleTag",
        "setContent",
        "exposeBinding",
        "exposeFunction",
        "execute_javascript",
        "execute_js",
        "run_javascript",
    ),
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


MAX_INJECTED_CHARS: Final = 200
"""Longest piece of injected code kept on an INJECT action's value."""


def injected(api: str, code: str | None) -> str:
    """An INJECT action's value: the API the agent called and its code, cut to
    MAX_INJECTED_CHARS (`evaluate(document.title = 'x')`)."""
    text = " ".join((code or "").split())
    if len(text) > MAX_INJECTED_CHARS:
        text = text[: MAX_INJECTED_CHARS - 1] + "…"
    return f"{api}({text})"
