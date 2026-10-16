"""A tree scope's stylesheets: their rules indexed by key and read with layers, ``@import``,
conditions and nesting, and the cascade that picks each property's value for an element."""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from importlib import resources
from typing import Final
from urllib.parse import urljoin

from evidence_judge.ingest.css.conditions import media_holds, supports
from evidence_judge.ingest.css.matching import matches
from evidence_judge.ingest.css.model import (
    HTML,
    MATHML,
    NO,
    SVG,
    YES,
    Compound,
    Computed,
    Element,
    Rank,
    Rule,
    Selector,
    Sheet,
    Viewport,
    words,
)
from evidence_judge.ingest.css.selector import (
    GENERATED,
    INVALID,
    UNKNOWN,
    fallback_selector,
    nested_selector,
    parse_complex,
)
from evidence_judge.ingest.css.syntax import (
    blocks,
    closing_paren,
    declarations,
    generates_text,
    parse_declaration,
    split_commas,
)
from evidence_judge.ingest.css.values import far_offset, length_px
from evidence_judge.ingest.css.visibility import Cascade, compute

_NAMESPACES: Final = {
    "http://www.w3.org/1999/xhtml": HTML,
    "http://www.w3.org/2000/svg": SVG,
    "http://www.w3.org/1998/Math/MathML": MATHML,
}


_UA_SECTION = re.compile(r'@namespace\s+"([^"]+)"\s*;')


UTILITY_CSS: Final = (
    ".hidden,.d-none,.sr-only,.visually-hidden{display:none}.invisible{visibility:hidden}"
)
"""Tailwind's and Bootstrap's hiding classes: a guess at what they do when the recording does
not hold a stylesheet that defines them."""


MAX_SHEET_CHARS: Final = 4 * 1024 * 1024
"""How much of each stylesheet is read; a longer one leaves the rest unknown."""


MAX_IMPORT_DEPTH: Final = 4
"""How deep ``@import`` chains are followed; a deeper one is unknown."""


StylesheetSource = Callable[[str], str | None]
"""The text of the stylesheet at an absolute URL as the recording holds it ("" when the
recording shows it did not load), or None when the recording does not hold it."""


_TOP: Final = 1 << 30
"""The layer key of unlayered rules: after every layer."""


_PRESENTATION: Final = words("display visibility opacity fill fill-opacity")
"""SVG presentation attributes the reader applies: author-level, beneath every rule."""


_SKIPPED_AT_RULES: Final = words(
    "font-face keyframes -webkit-keyframes page counter-style property "
    "font-feature-values font-palette-values view-transition position-try namespace "
    "charset starting-style document -moz-document"
)
"""At-rules whose rules never style an element's final state (or that Chromium ignores)."""


_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.DOTALL)


_AT_RULE = re.compile(r"@([\w-]+)\s*(.*)", re.DOTALL)


@dataclass(slots=True)
class _Bucket:
    """The rules whose rightmost compound has one key, split by a key an ancestor must have."""

    free: list[Rule] = field(default_factory=list["Rule"])
    by_ancestor: dict[str, list[Rule]] = field(default_factory=dict[str, list["Rule"]])


@dataclass(frozen=True, slots=True)
class _Context:
    """Where a rule is read: under what uncertain condition, in what layer, from what URL."""

    url: str | None = None
    maybe: bool = False
    layer: tuple[str, ...] = ()
    depth: int = 0


class StyleSheets:
    """The rules of one tree scope's stylesheets (the document, or one shadow root), indexed by
    the key of their rightmost compound selector."""

    def __init__(
        self,
        sheets: Iterable[Sheet],
        viewport: Viewport | None,
        *,
        source: StylesheetSource | None = None,
        document: bool = True,
        user_agent: bool = False,
    ) -> None:
        self._viewport = viewport
        self._user_agent = user_agent
        self._source = source
        self._by_key: dict[str, _Bucket] = {}
        self._order = 0
        self._layers: dict[tuple[str, ...], int] = {}
        self._layer_children: dict[tuple[str, ...], int] = {}
        self._anonymous = 0
        self.unknown = False
        """The scope's styles are partly unknown (a sheet cut short, or an ``@import`` the
        recording does not hold): any element may be hidden."""
        self.parts = False
        """A ``::part()`` rule may hide elements inside shadow trees."""
        self.slotted = False
        """A ``::slotted()`` rule may hide the elements assigned to this scope's slots."""
        self.details = False
        """A ``::details-content`` rule may show or hide closed ``<details>`` bodies."""
        self.generators: list[Selector] = []
        """The elements a ``::before`` or ``::after`` rule gives text (`generates_text`)."""
        for sheet in sheets:
            self._sheet(sheet.text, _Context(sheet.url, sheet.maybe))
        if document:
            self._guess_utilities()

    def generates(self, element: Element) -> bool:
        """A ``::before`` or ``::after`` rule may give `element` text the snapshot does not
        hold."""
        return any(matches(selector, element) != NO for selector in self.generators)

    def style(
        self,
        element: Element,
        parent: Computed,
        *,
        ua: bool = True,
        host: StyleSheets | None = None,
        proxy: Element | None = None,
    ) -> Computed:
        """The cascade for `element` under `parent`: the browser's own hiding rules for its
        namespace (`ua_hiding.css`) unless `ua` is False, SVG presentation attributes, and this
        scope's rules. `host` and `proxy` are the shadow scope `element` hosts and its stand-in
        there, for ``:host`` rules."""
        best: dict[str, Rule] = {}
        maybe: dict[str, list[Rule]] = {}
        if ua and (own := user_agent_sheets().get(element.namespace)) is not None:
            for rule in own._candidates(element):
                _offer(rule, matches(rule.selector, element), best, maybe)
        if element.namespace == SVG:
            for prop in _PRESENTATION.intersection(element.attrs):
                rank: Rank = (1, 1, 0, (-2,), (0, 0, 0), 0)
                value = element.attrs[prop].strip().lower()
                _offer(Rule(_UNIVERSAL, prop, value, rank, False), YES, best, maybe)
        for rule in self._candidates(element):
            _offer(rule, matches(rule.selector, element), best, maybe)
        if host is not None and proxy is not None:
            for rule in host._candidates(proxy):
                inner = rule.rank
                context = 2 if inner[0] >= 2 else 0
                moved = replace(rule, rank=(inner[0], context, *inner[2:]))
                _offer(moved, matches(rule.selector, proxy), best, maybe)
        for prop, value, important in declarations(element.attrs.get("style") or ""):
            rank = (2 if important else 1, 1, 1, (_TOP,), (0, 0, 0), 0)
            _offer(Rule(_UNIVERSAL, prop, value, rank, False), YES, best, maybe)
        svg = element.namespace == SVG
        if not best and not maybe:
            return parent if parent.svg == svg else replace(parent, svg=svg)
        present = frozenset(best).union(maybe)
        computed = compute(Cascade(best, maybe, present), parent, self._viewport)
        return computed if computed.svg == svg or computed.removed else replace(computed, svg=svg)

    def collapsed(self, element: Element) -> bool:
        """The page's styles certainly give `element` a box at most a pixel wide or high, or
        place it far off-screen (a 0x0 analytics frame, one parked at ``left: -9999px``)."""
        best: dict[str, Rule] = {}
        for rule in self._candidates(element):
            if matches(rule.selector, element) == YES and not rule.maybe:
                _offer(rule, YES, best, {})
        for prop, value, important in declarations(element.attrs.get("style") or ""):
            rank: Rank = (2 if important else 1, 1, 1, (_TOP,), (0, 0, 0), 0)
            _offer(Rule(_UNIVERSAL, prop, value, rank, False), YES, best, {})
        values = {prop: rule.value for prop, rule in best.items() if rule.value is not None}
        for prop in ("width", "height"):
            px = length_px(values.get(prop, ""), self._viewport)
            if px is not None and px <= 1:
                return True
        if values.get("position") not in ("absolute", "fixed"):
            return False
        return any(
            far_offset(values.get(prop), self._viewport, sign)
            for prop, sign in (("left", 0), ("right", 0), ("top", -1), ("bottom", 1))
        )

    def _candidates(self, element: Element) -> Iterator[Rule]:
        for key in ("*", *element.keys):
            bucket = self._by_key.get(key)
            if bucket is None:
                continue
            yield from bucket.free
            if not bucket.by_ancestor:
                continue
            ancestry = element.ancestry
            if len(ancestry) <= len(bucket.by_ancestor):
                for found in ancestry:
                    yield from bucket.by_ancestor.get(found, ())
            else:
                for found, rules in bucket.by_ancestor.items():
                    if found in ancestry:
                        yield from rules

    def _guess_utilities(self) -> None:
        """Add the utility hiding classes no sheet of the scope defines, as rules that may apply,
        below every rule of the site's."""
        for prelude, body in blocks(UTILITY_CSS):
            for text in split_commas(prelude):
                selector, _, _ = parse_complex(text)
                if selector is None or _key(selector.parts[-1]) in self._by_key:
                    continue
                for prop, value, _important in parse_declaration(body or ""):
                    self._order += 1
                    rank: Rank = (1, 1, 0, (-1,), selector.specificity, self._order)
                    self._index(Rule(selector, prop, value, rank, True))

    def _sheet(self, text: str, context: _Context) -> None:
        if len(text) > MAX_SHEET_CHARS:
            self.unknown = True
        self._read(_COMMENT.sub(" ", text[:MAX_SHEET_CHARS]), [], context)

    def _read(self, text: str, parents: list[Selector], context: _Context) -> None:
        for prelude, body in blocks(text):
            at_rule = _AT_RULE.match(prelude)
            if body is None:
                if parents:
                    self._declare(parents, prelude, context)
                elif at_rule is not None:
                    self._statement(at_rule.group(1).lower(), at_rule.group(2), context)
                continue
            if at_rule is not None:
                self._block(at_rule.group(1).lower(), at_rule.group(2), body, parents, context)
                continue
            selectors = self._selectors(prelude, parents, body)
            if selectors:
                self._read(body, selectors, context)

    def _statement(self, name: str, condition: str, context: _Context) -> None:
        if name == "layer":
            for layer in split_commas(condition):
                self._layer((*context.layer, *layer.split(".")))
        elif name == "import":
            self._import(condition, context)

    def _block(
        self, name: str, condition: str, body: str, parents: list[Selector], context: _Context
    ) -> None:
        if name in _SKIPPED_AT_RULES:
            return
        if name == "layer":
            if condition.strip():
                path = (*context.layer, *condition.strip().split("."))
            else:
                self._anonymous += 1
                path = (*context.layer, f"\0{self._anonymous}")
            self._layer(path)
            self._read(body, parents, replace(context, layer=path))
            return
        if name == "media":
            holds = media_holds(condition, self._viewport)
        elif name == "supports":
            holds = supports(condition)
        else:
            holds = None
        if holds is not False:
            self._read(body, parents, replace(context, maybe=context.maybe or holds is None))

    def _import(self, statement: str, context: _Context) -> None:
        target = _import_target(statement)
        if target is None:
            return
        href, rest = target
        layer = context.layer
        if (named := re.match(r"layer\(\s*([^)]*?)\s*\)\s*", rest)) is not None:
            layer = (*layer, *named.group(1).split("."))
            rest = rest[named.end() :]
        elif (bare := re.match(r"layer\b\s*", rest)) is not None:
            self._anonymous += 1
            layer = (*layer, f"\0{self._anonymous}")
            rest = rest[bare.end() :]
        holds: bool | None = True
        if rest.startswith("supports("):
            close = closing_paren(rest, len("supports("))
            tested = rest[len("supports(") : close]
            if ":" in tested and not tested.lstrip().startswith(("(", "not", "selector")):
                tested = f"({tested})"
            holds = None if close is None else supports(tested)
            rest = "" if close is None else rest[close + 1 :].strip()
        if rest and holds is not False:
            media = media_holds(rest, self._viewport)
            holds = False if media is False else (holds if media else None)
        if holds is False:
            return
        self._layer(layer)
        text = None
        url: str | None = None
        if self._source is not None and context.depth < MAX_IMPORT_DEPTH:
            try:
                url = urljoin(context.url or "", href)
            except ValueError:
                url = None
            text = None if url is None else self._source(url)
        if text is None:
            self.unknown = True
            return
        self._sheet(text, _Context(url, context.maybe or holds is None, layer, context.depth + 1))

    def _layer(self, path: tuple[str, ...]) -> None:
        for end in range(1, len(path) + 1):
            prefix = path[:end]
            if prefix not in self._layers:
                parent = prefix[:-1]
                self._layers[prefix] = self._layer_children.get(parent, 0)
                self._layer_children[parent] = self._layers[prefix] + 1

    def _layer_key(self, path: tuple[str, ...]) -> tuple[int, ...]:
        return (*(self._layers[path[: end + 1]] for end in range(len(path))), _TOP)

    def _selectors(self, prelude: str, parents: list[Selector], body: str) -> list[Selector]:
        out: list[Selector] = []
        uncertain = False
        for text in split_commas(prelude):
            if parents:
                for parent in parents:
                    nested = nested_selector(text, parent)
                    if nested is None:
                        nested = fallback_selector(text.replace("&", "*"))[0] or _UNIVERSAL
                        uncertain = True
                    out.append(nested)
                continue
            selector, validity, flag = parse_complex(text)
            if validity == INVALID:
                return []
            uncertain = uncertain or validity == UNKNOWN
            if flag == GENERATED:
                if selector is not None and generates_text(body):
                    self.generators.append(selector)
                continue
            if flag and any(True for _ in declarations(body)):
                self._flag(flag)
            if selector is not None:
                out.append(selector)
        if uncertain:
            out = [replace(s, maybe=True) for s in out]
        return out

    def _flag(self, flag: str) -> None:
        if flag == "part":
            self.parts = True
        elif flag == "slotted":
            self.slotted = True
        elif flag == "details":
            self.details = True

    def _declare(self, selectors: list[Selector], text: str, context: _Context) -> None:
        layer = self._layer_key(context.layer)
        normal, important_level = (0, 3) if self._user_agent else (1, 2)
        for prop, value, important in parse_declaration(text):
            for selector in selectors:
                self._order += 1
                level, key = (
                    (important_level, tuple(-k for k in layer)) if important else (normal, layer)
                )
                rank: Rank = (level, 1, 0, key, selector.specificity, self._order)
                self._index(Rule(selector, prop, value, rank, context.maybe))

    def _index(self, rule: Rule) -> None:
        selector = rule.selector
        bucket = self._by_key.setdefault(_key(selector.parts[-1]), _Bucket())
        ancestor = next(
            (
                k
                for part, combinator in zip(
                    reversed(selector.parts[:-1]), reversed(selector.combinators), strict=True
                )
                if combinator in " >" and (k := _key(part)) not in ("*", ":host")
            ),
            None,
        )
        if ancestor is None:
            bucket.free.append(rule)
        else:
            bucket.by_ancestor.setdefault(ancestor, []).append(rule)


_UNIVERSAL: Final = Selector((Compound(),), ())


@functools.cache
def user_agent_sheets() -> dict[str, StyleSheets]:
    """The browser's own hiding rules (`ua_hiding.css`, vendored from the HTML, SVG and MathML
    specifications by scripts/vendor_ua_hiding.py), by the namespace they apply to."""
    text = (
        resources.files("evidence_judge.ingest")
        .joinpath("ua_hiding.css")
        .read_text(encoding="utf-8")
    )
    parts = _UA_SECTION.split(_COMMENT.sub(" ", text))
    return {
        _NAMESPACES[uri]: StyleSheets([Sheet(body)], None, document=False, user_agent=True)
        for uri, body in zip(parts[1::2], parts[2::2], strict=True)
    }


def _offer(rule: Rule, matched: int, best: dict[str, Rule], maybe: dict[str, list[Rule]]) -> None:
    if matched == NO:
        return
    if matched == YES and not rule.maybe:
        current = best.get(rule.prop)
        if current is None or rule.rank > current.rank:
            best[rule.prop] = rule
    else:
        maybe.setdefault(rule.prop, []).append(rule)


def _import_target(statement: str) -> tuple[str, str] | None:
    """The URL an ``@import`` names and the conditions after it."""
    found = re.match(
        r"""(?:url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s]*))\s*\)|"([^"]*)"|'([^']*)')\s*(.*)""",
        statement.strip(),
        re.DOTALL,
    )
    if found is None:
        return None
    href = next((g for g in found.groups()[:5] if g is not None), "")
    return href, found.group(6).strip()


def _key(part: Compound) -> str:
    """The key a compound is indexed by: ``:host``, its id, else its last class (a modifier is
    rarer than the base class it modifies), else its tag."""
    if part.host:
        return ":host"
    if part.ids:
        return f"#{part.ids[0]}"
    if part.classes:
        return f".{part.classes[-1]}"
    return part.tag or "*"
