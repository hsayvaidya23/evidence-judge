"""Selector parsing: complex and nested selectors, pseudo-classes and pseudo-elements."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Final

from evidence_judge.ingest.css.model import Compound, Pseudo, Selector, words
from evidence_judge.ingest.css.syntax import split_commas, unescape
from evidence_judge.ingest.css.values import closing_paren

_VALID, UNKNOWN, INVALID = 0, 1, 2


_STATE_PSEUDOS: Final = words(
    "hover focus active focus-within focus-visible visited link target target-within "
    "checked disabled enabled default indeterminate required optional valid invalid "
    "user-valid user-invalid in-range out-of-range read-only read-write placeholder-shown "
    "autofill -webkit-autofill fullscreen modal popover-open open closed playing paused "
    "picture-in-picture empty local-link blank"
)
"""Pseudo-classes for a state the snapshot does not record: they may match."""


_PSEUDO_ELEMENTS: Final = words(
    "before after marker placeholder selection backdrop file-selector-button cue "
    "highlight spelling-error grammar-error target-text first-line first-letter part "
    "slotted details-content picker column scroll-marker scroll-button checkmark "
    "picker-icon"
)


_LEGACY_PSEUDO_ELEMENTS: Final = frozenset({"before", "after", "first-line", "first-letter"})


_IDENT = r"-?(?:[\w-]|\\[0-9a-fA-F]{1,6}[ \t\n]?|\\.)+"


_TOKEN = re.compile(
    rf"""(?P<space>\s+)|(?P<comb>[>+~])|(?P<star>\*)
    |(?P<tag>{_IDENT})|\#(?P<id>{_IDENT})|\.(?P<cls>{_IDENT})
    |\[\s*(?P<attr>{_IDENT})\s*(?:(?P<op>[~|^$*]?=)\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'
    |(?P<bare>{_IDENT}))\s*(?P<flag>[iIsS])?\s*)?\]
    |(?P<pseudo>::?{_IDENT})(?P<args>\()?""",
    re.VERBOSE,
)


_NTH = re.compile(
    r"^(?:(?P<odd>odd)|(?P<even>even)|(?P<a>[+-]?\d*)n\s*(?:(?P<sign>[+-])\s*(?P<b>\d+))?"
    r"|(?P<only>[+-]?\d+))$"
)


GENERATED: Final = "generated"


def nested_selector(text: str, parent: Selector) -> Selector | None:
    """A nested rule's selector under `parent`: a leading ``&`` stands for it (``&.open``,
    ``& > p``), and a selector without one is its descendant (``> p`` its child); None when the
    reader cannot read it."""
    if text.count("&") > 1 or ("&" in text and not text.startswith("&")):
        return None
    if text.startswith("&"):
        tail = text[1:]
        if not tail:
            return parent
        joined = tail[0] not in " \t\n>+~"
    else:
        tail, joined = (text if text[:1] in (">", "+", "~") else f" {text}"), False
    own, validity, _ = parse_complex(f"*{tail}")
    if own is None or validity != _VALID:
        return None
    maybe = parent.maybe or own.maybe
    if joined:
        merged = _merge(parent.parts[-1], own.parts[0])
        return Selector(
            (*parent.parts[:-1], merged, *own.parts[1:]),
            (*parent.combinators, *own.combinators),
            maybe,
        )
    return Selector((*parent.parts, *own.parts[1:]), (*parent.combinators, *own.combinators), maybe)


def _merge(a: Compound, b: Compound) -> Compound:
    return Compound(
        a.tag or b.tag,
        a.ids + b.ids,
        a.classes + b.classes,
        a.attrs + b.attrs,
        a.pseudos + b.pseudos,
        a.host or b.host,
        a.host_args + b.host_args,
        a.host_maybe or b.host_maybe,
    )


@dataclass(slots=True)
class _Builder:
    tag: str | None = None
    ids: list[str] = field(default_factory=list[str])
    classes: list[str] = field(default_factory=list[str])
    attrs: list[tuple[str, str | None, str, bool]] = field(
        default_factory=list[tuple[str, str | None, str, bool]]
    )
    pseudos: list[Pseudo] = field(default_factory=list[Pseudo])
    host: bool = False
    host_args: list[Compound] = field(default_factory=list[Compound])
    host_maybe: bool = False
    element: str | None = None
    """The pseudo-element the compound ends with."""

    def build(self) -> Compound:
        return Compound(
            self.tag,
            tuple(self.ids),
            tuple(self.classes),
            tuple(self.attrs),
            tuple(self.pseudos),
            self.host,
            tuple(self.host_args),
            self.host_maybe,
        )


def parse_complex(text: str) -> tuple[Selector | None, int, str]:
    """A complex selector, how sure the reader is that a browser accepts it (_VALID, UNKNOWN or
    INVALID), and the scope flag of a ``::part()``, ``::slotted()`` or ``::details-content``
    rule. The selector is None when it styles a pseudo-element instead of an element; when the
    reader cannot parse it, it is an approximation that may match (its last compound)."""
    text = text.strip()
    parts: list[Compound] = []
    combinators: list[str] = []
    found = _Builder()
    pending: str | None = None
    validity, maybe, flag = _VALID, False, ""
    seen = False
    at = 0
    while at < len(text):
        match = _TOKEN.match(text, at)
        if match is None:
            return fallback_selector(text)
        at = match.end()
        groups = match.groupdict()
        if groups["space"] is not None:
            pending = pending or " "
            continue
        if groups["comb"] is not None:
            if pending not in (None, " "):
                return None, INVALID, ""
            pending = groups["comb"]
            continue
        if pending is not None:
            if not seen or found.element is not None:
                return None, INVALID, ""
            parts.append(found.build())
            combinators.append(pending)
            found, pending = _Builder(), None
        seen = True
        if groups["star"] is not None:
            continue
        if groups["tag"] is not None:
            found.tag = unescape(groups["tag"]).upper()
        elif groups["id"] is not None:
            found.ids.append(unescape(groups["id"]))
        elif groups["cls"] is not None:
            found.classes.append(unescape(groups["cls"]))
        elif groups["attr"] is not None:
            value = groups["dq"] if groups["dq"] is not None else groups["sq"]
            if value is None and groups["bare"] is not None:
                value = unescape(groups["bare"])
            folded = (groups["flag"] or "").lower() == "i"
            found.attrs.append(
                (unescape(groups["attr"]).lower(), groups["op"], value or "", folded)
            )
        else:
            raw = groups["pseudo"].lower()
            name = raw.lstrip(":")
            args: str | None = None
            if groups["args"] is not None:
                close = closing_paren(text, at)
                if close is None:
                    return fallback_selector(text)
                args, at = text[at:close], close + 1
            element = raw.startswith("::") or name in _LEGACY_PSEUDO_ELEMENTS
            if element:
                if found.element is not None:
                    return None, INVALID, ""
                found.element = name
                if name in ("part", "slotted", "details-content"):
                    flag = {"details-content": "details"}.get(name, name)
                elif name not in _PSEUDO_ELEMENTS:
                    validity = max(validity, INVALID if foreign(name) else UNKNOWN)
                continue
            outcome = _pseudo_class(name, args, found)
            if outcome == INVALID:
                return None, INVALID, ""
            if outcome == UNKNOWN:
                validity, maybe = max(validity, UNKNOWN), True
    if not seen or pending not in (None, " "):
        return None, INVALID, ""
    parts.append(found.build())
    if found.element is not None:
        if found.element in ("first-line", "first-letter"):
            return Selector(tuple(parts), tuple(combinators), True), validity, flag
        if found.element in ("before", "after"):
            return Selector(tuple(parts), tuple(combinators), maybe), validity, GENERATED
        return None, validity, flag
    return Selector(tuple(parts), tuple(combinators), maybe), validity, flag


_STRUCTURAL: Final = words(
    "first-child last-child only-child first-of-type last-of-type only-of-type defined any-link"
)


def foreign(name: str) -> bool:
    """A vendor prefix Chromium rejects."""
    return name.startswith(("-moz-", "-ms-", "-o-"))


def _pseudo_class(name: str, args: str | None, found: _Builder) -> int:
    """Add pseudo-class `name` (with `args`) to `found`; how sure the reader is it is valid."""
    if args is None:
        if name in ("root", "scope"):
            found.pseudos.append(Pseudo("root"))
        elif name in _STRUCTURAL:
            found.pseudos.append(Pseudo(name))
        elif name == "host":
            found.host = True
        elif name in _STATE_PSEUDOS:
            found.pseudos.append(Pseudo("maybe"))
        elif foreign(name):
            return INVALID
        else:
            found.pseudos.append(Pseudo("maybe"))
            return UNKNOWN
        return _VALID
    if name in ("not", "is", "where", "matches", "-webkit-any"):
        forgiving = name != "not"
        selectors: list[Selector] = []
        unsure = False
        for text in split_commas(args):
            selector, validity, _ = parse_complex(text)
            if validity == INVALID:
                if not forgiving:
                    return INVALID
                continue
            unsure = unsure or validity == UNKNOWN
            if selector is not None:
                selectors.append(selector)
        kind = "not" if name == "not" else ("where" if name == "where" else "is")
        found.pseudos.append(Pseudo(kind, tuple(selectors), maybe=unsure))
        return UNKNOWN if unsure and not forgiving else _VALID
    if name in ("nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type"):
        formula, _, of = args.partition(" of ")
        nth = _nth(formula)
        if nth is None:
            return INVALID
        found.pseudos.append(Pseudo("maybe") if of.strip() else Pseudo(name, nth=nth))
        return _VALID
    if name == "host":
        found.host = True
        compound, validity, _ = parse_complex(args)
        if compound is None or len(compound.parts) != 1 or validity != _VALID:
            found.host_maybe = True
        else:
            found.host_args.append(compound.parts[0])
        return _VALID
    if name == "host-context":
        found.host, found.host_maybe = True, True
        return _VALID
    if name in ("has", "lang", "dir", "state", "active-view-transition-type"):
        found.pseudos.append(Pseudo("maybe"))
        return _VALID
    if foreign(name):
        return INVALID
    found.pseudos.append(Pseudo("maybe"))
    return UNKNOWN


def _nth(formula: str) -> tuple[int, int] | None:
    found = _NTH.match(formula.strip().lower())
    if found is None:
        return None
    if found.group("odd"):
        return 2, 1
    if found.group("even"):
        return 2, 0
    if found.group("only") is not None:
        return 0, int(found.group("only"))
    a = found.group("a")
    step = 1 if a in ("", "+") else (-1 if a == "-" else int(a))
    offset = int(found.group("b") or 0) * (-1 if found.group("sign") == "-" else 1)
    return step, offset


def fallback_selector(text: str) -> tuple[Selector | None, int, str]:
    """An approximation of a selector the reader cannot parse: its last compound, which may
    match; any element when that cannot be read either."""
    depth, cut = 0, 0
    for i, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif depth == 0 and char in " >+~":
            cut = i + 1
    tail = text[cut:].strip()
    if tail and tail != text:
        selector, validity, _ = parse_complex(tail)
        if selector is not None and validity != INVALID and len(selector.parts) == 1:
            return replace(selector, maybe=True), UNKNOWN, ""
    return Selector((Compound(),), (), True), UNKNOWN, ""
