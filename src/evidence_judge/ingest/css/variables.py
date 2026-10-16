"""Custom properties and ``var()``: what each element's custom properties hold, and the values
of the properties that use them."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Final

from evidence_judge.ingest.css.model import UNSET, Registered, Registration, Rule
from evidence_judge.ingest.css.values import MAX_NESTING, closing_paren, split_args

Values = tuple[str, ...]
"""The values a custom property may hold (more than one when a rule that may apply, such as a
``:hover`` rule, sets it)."""


Custom = Mapping[str, Values | None]
"""An element's custom properties: the values each may hold with every ``var()`` substituted,
None when the reader cannot tell. A property it does not hold is undefined."""


MAX_VALUE_CHARS: Final = 8192
"""How long a substituted value may grow before the reader gives up on it."""


MAX_ALTERNATIVES: Final = 16
"""How many values a substituted value may take before the reader gives up on it."""


class _InvalidError(Exception):
    """A value is invalid at computed-value time: a ``var()`` names an undefined custom
    property and has no fallback."""


class _UnknownError(Exception):
    """The reader cannot tell what a value substitutes to."""


_REFERENCE = re.compile(r"\b(var|env)\(")


_ENV_ZERO: Final = re.compile(r"(safe-area-inset|safe-area-max-inset|keyboard-inset)-")
"""Environment variables that are 0 on the desktop page a recording shows."""


_Lookup = Callable[[str, int], Values]
"""The values a custom property holds, looked up `depth` substitutions deep; raises
`_InvalidError` when it is undefined and `_UnknownError` when the reader cannot tell."""


def custom_properties(
    candidates: Mapping[str, tuple[str | None, ...]],
    inherited: Custom,
    registered: Registered,
) -> tuple[Custom, Custom]:
    """The custom properties of an element: those its children inherit, and its own values of
    the registered properties that do not inherit. `candidates` holds the values each property
    it sets may take (UNSET: none of its rules applies)."""
    own: dict[str, Values | None] = {}
    undefined: set[str] = set()
    resolving: list[str] = []
    cyclic: set[str] = set()

    def lookup(name: str, depth: int) -> Values:
        if name in undefined:
            raise _InvalidError
        if name in own:
            return _known(own[name])
        if name not in candidates:
            return _held(name, inherited, {}, registered)
        if name in resolving:
            cyclic.update(resolving[resolving.index(name) :])
            raise _InvalidError
        if depth > MAX_NESTING:
            own[name] = None
            raise _UnknownError
        resolving.append(name)
        try:
            found = _choose(name, candidates[name], inherited, registered, lookup, depth + 1)
        except _UnknownError:
            own[name] = None
            raise
        finally:
            resolving.pop()
        if found is None or name in cyclic:
            undefined.add(name)
            raise _InvalidError
        own[name] = found
        return found

    for name in candidates:
        with suppress(_InvalidError, _UnknownError):
            lookup(name, 0)
    local = {n: v for n, v in own.items() if not _inherits(registered.get(n, _UNREGISTERED))}
    passed = {n: v for n, v in own.items() if n not in local}
    gone = [n for n in undefined if n in inherited]
    if not passed and not gone:
        return inherited, local
    forward = {**inherited, **passed}
    for name in gone:
        del forward[name]
    return forward, local


_UNREGISTERED: Final = Registration(True, None)
"""How an unregistered custom property behaves: it inherits, and is undefined until set."""


def _inherits(registration: Registration | None) -> bool:
    return registration is None or registration.inherits


def _known(values: Values | None) -> Values:
    if values is None:
        raise _UnknownError
    return values


def _held(name: str, inherited: Custom, local: Custom, registered: Registered) -> Values:
    """What a custom property an element does not set holds there."""
    if name in local:
        return _known(local[name])
    registration = registered.get(name, _UNREGISTERED)
    if registration is None:
        raise _UnknownError
    found = _default(name, inherited, registration, registration.inherits)
    if found is None:
        raise _InvalidError
    return found


def _default(
    name: str, inherited: Custom, registration: Registration, inherit: bool
) -> Values | None:
    """A custom property's inherited value (when `inherit`) or initial value; None when it is
    undefined."""
    if inherit and name in inherited:
        return _known(inherited[name])
    return None if registration.initial is None else (registration.initial,)


def _choose(
    name: str,
    values: tuple[str | None, ...],
    inherited: Custom,
    registered: Registered,
    lookup: _Lookup,
    depth: int,
) -> Values | None:
    """The values a custom property may take (None: it is undefined)."""
    registration = registered.get(name, _UNREGISTERED)
    if registration is None:
        raise _UnknownError
    found: list[str] = []
    undefined = False
    for value in values:
        if value is None or value in ("revert", "revert-layer"):
            raise _UnknownError
        if value in (UNSET, "unset", "inherit", "initial"):
            if value == "inherit" and not registration.inherits:
                raise _UnknownError
            inherit = value != "initial" and registration.inherits
            held = _default(name, inherited, registration, inherit)
            if held is None:
                undefined = True
            else:
                found.extend(held)
            continue
        try:
            found.extend(_substitute(value, lookup, depth))
        except _InvalidError:
            undefined = True
    if undefined:
        if found:
            raise _UnknownError
        return None
    return _distinct(found)


def references(value: str | None) -> bool:
    if value is None or ("var(" not in value and "env(" not in value):
        return False
    return _REFERENCE.search(value) is not None


def resolve(value: str, custom: Custom, local: Custom, registered: Registered) -> Values | None:
    """The values `value` may take with every ``var()`` and ``env()`` substituted: ``unset``
    when it is invalid at computed-value time, None when the reader cannot tell."""
    try:
        return _substitute(value, lambda name, _depth: _held(name, custom, local, registered), 0)
    except _InvalidError:
        return ("unset",)
    except _UnknownError:
        return None


def resolve_rule(rule: Rule, custom: Custom, local: Custom, registered: Registered) -> list[Rule]:
    """`rule` with its value substituted: one rule for each value it may take."""
    if rule.value is None or not references(rule.value):
        return [rule]
    found = resolve(rule.value, custom, local, registered)
    if found is None:
        return [replace(rule, value=None)]
    return [replace(rule, value=value) for value in found]


def _substitute(value: str, lookup: _Lookup, depth: int) -> Values:
    """`value` with its references substituted, `depth` substitutions deep: past MAX_NESTING
    the reader cannot tell."""
    if depth > MAX_NESTING:
        raise _UnknownError
    out: Values = ("",)
    at = 0
    while (found := _REFERENCE.search(value, at)) is not None:
        close = closing_paren(value, found.end())
        if close is None:
            raise _UnknownError
        args = split_args(value[found.end() : close])
        name = args[0].strip()
        fallback = ",".join(args[1:]).strip() if len(args) > 1 else None
        if found.group(1) == "env":
            if _ENV_ZERO.match(name):
                held: Values = ("0px",)
            elif fallback is not None:
                held = _substitute(fallback, lookup, depth + 1)
            else:
                raise _UnknownError
        else:
            if not name.startswith("--"):
                raise _UnknownError
            try:
                held = lookup(name, depth)
            except _InvalidError:
                if fallback is None:
                    raise
                held = _substitute(fallback, lookup, depth + 1)
        before = value[at : found.start()]
        out = _distinct([f"{o}{before}{h}" for o in out for h in held])
        at = close + 1
    return _distinct([" ".join(f"{o}{value[at:]}".split()) for o in out])


def _distinct(values: list[str]) -> Values:
    found = tuple(dict.fromkeys(values))
    if len(found) > MAX_ALTERNATIVES or any(len(v) > MAX_VALUE_CHARS for v in found):
        raise _UnknownError
    return found
