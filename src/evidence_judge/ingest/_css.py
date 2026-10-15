"""Whether a snapshot's styles show an element: a CSS reader for page text that knows what it
does not know.

It applies the rules of a tree scope's stylesheets with the cascade (origin, ``!important``,
shadow context, inline styles, ``@layer`` order, specificity, order) and answers, for each
element, three ways: its text shows, the styles hide it (only when the reader is certain), or
the reader is unsure. Unsure is the answer whenever visibility depends on something it does not
fully model:

- a rule under a condition it cannot evaluate: an ``@media`` query it cannot read (resolution,
  ``calc()``), an ``@supports`` test it does not know the answer to, ``@container``,
  ``@scope`` or an unknown at-rule; a stylesheet ``@import``s a sheet the recording does not
  hold (then every element is unsure);
- a selector it cannot evaluate: user-action and form-state pseudo-classes (``:hover``,
  ``:checked``), ``:has()``, ``:empty``, ``:lang()``, unknown pseudo-classes, and selectors it
  cannot parse (matched on their last compound). Combinators (descendant, child, both sibling
  combinators), ``:not()``, ``:is()``, ``:where()``, the ``:nth-*`` and ``*-child`` /
  ``*-of-type`` pseudo-classes, ``:root``, ``:defined`` on built-in elements, ``:any-link``,
  ``:host`` / ``:host()`` and escapes are evaluated;
- a value it cannot read: ``var()``, ``env()``, ``attr()`` or an unknown keyword in
  ``display``, ``visibility`` or ``content-visibility``;
- styles that can make text unreadable without removing it: ``opacity`` near 0, a tiny
  ``font-size``, a transparent text colour or one equal to the background, ``clip`` /
  ``clip-path`` that cut it away, a ``transform`` that scales it to nothing, far off-screen
  offsets, margins or ``text-indent``, and a zero-size box that clips its overflow.

Utility hiding classes (`UTILITY_CSS`) whose stylesheet is not in the recording make an element
unsure, not hidden: the site may not load the framework that defines them.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from importlib import resources
from typing import Final
from urllib.parse import urljoin

NO: Final = 0
MAYBE: Final = 1
YES: Final = 2
"""Whether a selector matches or a condition holds: NO, MAYBE (the reader cannot tell), YES.
They order so that ``min`` is "and" and ``max`` is "or"."""

SHOWN: Final = 0
UNSURE: Final = 1
HIDDEN: Final = 2
"""Whether text shows: for certain, maybe, or certainly not."""

HTML: Final = "html"
SVG: Final = "svg"
MATHML: Final = "math"
"""Element namespaces."""
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
Viewport = tuple[int, int]
Rgba = tuple[float, float, float, float]

_TOP: Final = 1 << 30
"""The layer key of unlayered rules: after every layer."""
_UNSET: Final = ""
"""A property no rule sets."""
_VALID, _UNKNOWN, _INVALID = 0, 1, 2


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_TRACKED: Final = _words(
    "display visibility content-visibility opacity filter transform scale translate zoom "
    "clip clip-path position left right top bottom margin-left margin-top "
    "margin-inline-start margin-block-start height max-height width max-width block-size "
    "max-block-size inline-size max-inline-size overflow-x overflow-y font-size color "
    "-webkit-text-fill-color text-indent background-color background-clip "
    "-webkit-background-clip fill fill-opacity"
)
"""The properties the reader applies: what removes an element, and what can hide its text."""
_PRESENTATION: Final = _words("display visibility opacity fill fill-opacity")
"""SVG presentation attributes the reader applies: author-level, beneath every rule."""
_SHORTHANDS: Final = frozenset({"all", "font", "background", "inset", "margin", "overflow"})
_OBSCURERS: Final = _words("opacity filter transform scale translate zoom clip-path")
_SIZES: Final = _words(
    "height max-height width max-width block-size max-block-size inline-size max-inline-size"
)
_MARGINS: Final = _words("margin-left margin-top margin-inline-start margin-block-start")
_DISPLAY: Final = _words(
    "block inline inline-block flex inline-flex grid inline-grid flow-root flow contents "
    "list-item table inline-table table-row table-cell table-row-group table-header-group "
    "table-footer-group table-column table-column-group table-caption ruby ruby-text "
    "ruby-base math run-in -webkit-box -webkit-inline-box inherit initial unset"
)
_SKIPPED_AT_RULES: Final = _words(
    "font-face keyframes -webkit-keyframes page counter-style property "
    "font-feature-values font-palette-values view-transition position-try namespace "
    "charset starting-style document -moz-document"
)
"""At-rules whose rules never style an element's final state (or that Chromium ignores)."""
_STATE_PSEUDOS: Final = _words(
    "hover focus active focus-within focus-visible visited link target target-within "
    "checked disabled enabled default indeterminate required optional valid invalid "
    "user-valid user-invalid in-range out-of-range read-only read-write placeholder-shown "
    "autofill -webkit-autofill fullscreen modal popover-open open closed playing paused "
    "picture-in-picture empty local-link blank"
)
"""Pseudo-classes for a state the snapshot does not record: they may match."""
_PSEUDO_ELEMENTS: Final = _words(
    "before after marker placeholder selection backdrop file-selector-button cue "
    "highlight spelling-error grammar-error target-text first-line first-letter part "
    "slotted details-content picker column scroll-marker scroll-button checkmark "
    "picker-icon"
)
_LEGACY_PSEUDO_ELEMENTS: Final = frozenset({"before", "after", "first-line", "first-letter"})
_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.DOTALL)
_SPECIAL = re.compile(r"[{};\"'\\]")
_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6}[ \t\n]?|.)", re.DOTALL)
_IDENT = r"-?(?:[\w-]|\\[0-9a-fA-F]{1,6}[ \t\n]?|\\.)+"
_TOKEN = re.compile(
    rf"""(?P<space>\s+)|(?P<comb>[>+~])|(?P<star>\*)
    |(?P<tag>{_IDENT})|\#(?P<id>{_IDENT})|\.(?P<cls>{_IDENT})
    |\[\s*(?P<attr>{_IDENT})\s*(?:(?P<op>[~|^$*]?=)\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'
    |(?P<bare>{_IDENT}))\s*(?P<flag>[iIsS])?\s*)?\]
    |(?P<pseudo>::?{_IDENT})(?P<args>\()?""",
    re.VERBOSE,
)
_AT_RULE = re.compile(r"@([\w-]+)\s*(.*)", re.DOTALL)
_NUMBER = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)([a-z%]*)$")
_FUNCTION = re.compile(r"([a-z0-9-]+)\(([^()]*)\)")
_NTH = re.compile(
    r"^(?:(?P<odd>odd)|(?P<even>even)|(?P<a>[+-]?\d*)n\s*(?:(?P<sign>[+-])\s*(?P<b>\d+))?"
    r"|(?P<only>[+-]?\d+))$"
)
_RANGE = re.compile(
    r"^(?:([^<>=]+?)\s*([<>]=?|=))?\s*(width|height)\s*(?:([<>]=?|=)\s*([^<>=]+))?$"
)
_FLIPPED: Final = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "="}
_UNITS: Final = {
    "px": 1.0,
    "pt": 4 / 3,
    "pc": 16.0,
    "in": 96.0,
    "cm": 96 / 2.54,
    "mm": 96 / 25.4,
    "q": 96 / 101.6,
    "rem": 16.0,
}
_FAR: Final = 1000.0
"""How far (CSS pixels) an offset, margin or indent moves text before the reader is unsure it
is on screen."""
_FAINT: Final = 0.05
"""An opacity or scale at or below which the reader is unsure text can be seen."""
_TINY_FONT: Final = 4.0
_NAMED: Final[dict[str, Rgba]] = {
    "transparent": (0, 0, 0, 0),
    "black": (0, 0, 0, 1),
    "white": (255, 255, 255, 1),
    "red": (255, 0, 0, 1),
    "green": (0, 128, 0, 1),
    "blue": (0, 0, 255, 1),
    "gray": (128, 128, 128, 1),
    "grey": (128, 128, 128, 1),
    "silver": (192, 192, 192, 1),
    "yellow": (255, 255, 0, 1),
    "orange": (255, 165, 0, 1),
    "purple": (128, 0, 128, 1),
    "navy": (0, 0, 128, 1),
}
BLACK: Final[Rgba] = (0, 0, 0, 1)
WHITE: Final[Rgba] = (255, 255, 255, 1)

type _Rank = tuple[int, int, int, tuple[int, ...], tuple[int, int, int], int]
"""(origin and importance, shadow context, inline, layer, specificity, order): larger wins."""


@dataclass(frozen=True, slots=True)
class Sheet:
    """One stylesheet of a tree scope, in document order."""

    text: str
    url: str | None = None
    """Where it was loaded from (the document URL for a ``<style>``): what its ``@import``
    URLs resolve against."""
    maybe: bool = False
    """It applies only under a condition the reader cannot evaluate (its ``media``)."""


@dataclass(frozen=True, slots=True)
class _Pseudo:
    name: str
    args: tuple[_Selector, ...] = ()
    nth: tuple[int, int] | None = None
    maybe: bool = False
    """An argument could not be read: the pseudo-class may match where it says it does not."""

    @property
    def specificity(self) -> tuple[int, int, int]:
        if self.name == "where":
            return (0, 0, 0)
        if self.name in ("not", "is"):
            return max((a.specificity for a in self.args), default=(0, 0, 0))
        return (0, 1, 0)


@dataclass(frozen=True, slots=True)
class _Compound:
    tag: str | None = None
    ids: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()
    attrs: tuple[tuple[str, str | None, str, bool], ...] = ()
    pseudos: tuple[_Pseudo, ...] = ()
    host: bool = False
    """``:host``: matches only the shadow host, from inside its shadow tree."""
    host_args: tuple[_Compound, ...] = ()
    host_maybe: bool = False

    @property
    def specificity(self) -> tuple[int, int, int]:
        a, b, c = len(self.ids), len(self.classes) + len(self.attrs), int(bool(self.tag))
        b += int(self.host)
        for found in (
            *(p.specificity for p in self.pseudos),
            *(h.specificity for h in self.host_args),
        ):
            a, b, c = a + found[0], b + found[1], c + found[2]
        return a, b, c


@dataclass(frozen=True, slots=True)
class _Selector:
    parts: tuple[_Compound, ...]
    """Compound selectors from left to right."""
    combinators: tuple[str, ...]
    """Between consecutive parts: " " (descendant), ">", "+" or "~"."""
    maybe: bool = False
    """The reader approximated the selector: where it matches, the real one may not."""

    @property
    def specificity(self) -> tuple[int, int, int]:
        total = (0, 0, 0)
        for part in self.parts:
            a, b, c = part.specificity
            total = (total[0] + a, total[1] + b, total[2] + c)
        return total


@dataclass(frozen=True, slots=True)
class _Rule:
    selector: _Selector
    prop: str
    value: str | None
    """None: a value the reader cannot read (``var()``, ...)."""
    rank: _Rank
    maybe: bool
    """It applies only under a condition the reader cannot evaluate."""


@dataclass(slots=True, eq=False)
class Element:
    """An element as the cascade sees it: its tag, attributes, parent and siblings."""

    tag: str
    attrs: dict[str, str]
    parent: Element | None
    siblings: list[Element] | None = None
    """The parent's element children, this one among them (None: unknown, as if alone)."""
    index: int = 0
    """Its position in `siblings`."""
    host: Element | None = None
    """Set on the stand-in for a shadow host inside its shadow tree: the host it stands for.
    Only ``:host`` matches it."""
    namespace: str = "html"
    """HTML, SVG or MATHML: which of the browser's own stylesheets (`ua_hiding.css`) apply."""
    classes: frozenset[str] = field(init=False)
    keys: frozenset[str] = field(init=False)
    """What a rule is indexed by: its tag, ``.class`` for each class and ``#id``."""
    _lineage: frozenset[str] | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.classes = frozenset((self.attrs.get("class") or "").split())
        if self.host is not None:
            self.keys = frozenset({":host"})
            return
        keys = {self.tag, *(f".{c}" for c in self.classes)}
        if element_id := self.attrs.get("id"):
            keys.add(f"#{element_id}")
        self.keys = frozenset(keys)

    @property
    def ancestry(self) -> frozenset[str]:
        """The keys of every ancestor."""
        return self.parent.lineage if self.parent is not None else frozenset()

    @property
    def lineage(self) -> frozenset[str]:
        """The keys of this element and every ancestor, computed once."""
        if self._lineage is None:
            self._lineage = self.ancestry | self.keys
        return self._lineage

    @property
    def previous(self) -> Element | None:
        if self.siblings is None or self.index == 0:
            return None
        return self.siblings[self.index - 1]


@dataclass(frozen=True, slots=True)
class Computed:
    """What the cascade decides for an element, with what its children inherit."""

    removed: bool = False
    """``display: none`` or ``content-visibility: hidden`` for certain: the whole subtree."""
    unsure: bool = False
    """This element or an ancestor may be removed, or obscured: its subtree may not show."""
    visibility: int = SHOWN
    """Its computed ``visibility``: SHOWN, HIDDEN or UNSURE."""
    font_px: float | None = 16.0
    font_tiny: bool = False
    color: Rgba | None = BLACK
    clear: bool = False
    """Its text colour (or text fill) may be transparent."""
    background: Rgba | None = WHITE
    """The nearest opaque background behind it, None when not known."""
    clip_text: bool = False
    """A background shows through its text (``background-clip: text``)."""
    indent_far: bool = False
    fill_none: bool = False
    """Its SVG ``fill`` may paint nothing (``none``, a transparent colour)."""
    fill_faint: bool = False
    """Its SVG ``fill-opacity`` may be near 0."""
    svg: bool = False
    """It is an SVG element, whose text its ``fill`` paints."""

    @property
    def text(self) -> int:
        """Whether this element's own text shows: SHOWN, UNSURE or HIDDEN."""
        if self.visibility == HIDDEN:
            return HIDDEN
        faded = (
            not self.clip_text
            and (self.clear or (self.color is not None and self.color == self.background))
        ) or (self.svg and (self.fill_none or self.fill_faint))
        if self.unsure or self.visibility == UNSURE or self.font_tiny or self.indent_far or faded:
            return UNSURE
        return SHOWN


REMOVED: Final = Computed(removed=True)
ROOT: Final = Computed()


@dataclass(slots=True)
class _Bucket:
    """The rules whose rightmost compound has one key, split by a key an ancestor must have."""

    free: list[_Rule] = field(default_factory=list["_Rule"])
    by_ancestor: dict[str, list[_Rule]] = field(default_factory=dict[str, list["_Rule"]])


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
        self.generators: list[_Selector] = []
        """The elements a ``::before`` or ``::after`` rule gives text (`_generates_text`)."""
        for sheet in sheets:
            self._sheet(sheet.text, _Context(sheet.url, sheet.maybe))
        if document:
            self._guess_utilities()

    def generates(self, element: Element) -> bool:
        """A ``::before`` or ``::after`` rule may give `element` text the snapshot does not
        hold."""
        return any(_matches(selector, element) != NO for selector in self.generators)

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
        best: dict[str, _Rule] = {}
        maybe: dict[str, list[_Rule]] = {}
        if ua and (own := user_agent_sheets().get(element.namespace)) is not None:
            for rule in own._candidates(element):
                _offer(rule, _matches(rule.selector, element), best, maybe)
        if element.namespace == SVG:
            for prop in _PRESENTATION.intersection(element.attrs):
                rank: _Rank = (1, 1, 0, (-2,), (0, 0, 0), 0)
                value = element.attrs[prop].strip().lower()
                _offer(_Rule(_UNIVERSAL, prop, value, rank, False), YES, best, maybe)
        for rule in self._candidates(element):
            _offer(rule, _matches(rule.selector, element), best, maybe)
        if host is not None and proxy is not None:
            for rule in host._candidates(proxy):
                inner = rule.rank
                context = 2 if inner[0] >= 2 else 0
                moved = replace(rule, rank=(inner[0], context, *inner[2:]))
                _offer(moved, _matches(rule.selector, proxy), best, maybe)
        for prop, value, important in _declarations(element.attrs.get("style") or ""):
            rank = (2 if important else 1, 1, 1, (_TOP,), (0, 0, 0), 0)
            _offer(_Rule(_UNIVERSAL, prop, value, rank, False), YES, best, maybe)
        svg = element.namespace == SVG
        if not best and not maybe:
            return parent if parent.svg == svg else replace(parent, svg=svg)
        present = frozenset(best).union(maybe)
        computed = _compute(_Cascade(best, maybe, present), parent, self._viewport)
        return computed if computed.svg == svg or computed.removed else replace(computed, svg=svg)

    def collapsed(self, element: Element) -> bool:
        """The page's styles certainly give `element` a box at most a pixel wide or high, or
        place it far off-screen (a 0x0 analytics frame, one parked at ``left: -9999px``)."""
        best: dict[str, _Rule] = {}
        for rule in self._candidates(element):
            if _matches(rule.selector, element) == YES and not rule.maybe:
                _offer(rule, YES, best, {})
        for prop, value, important in _declarations(element.attrs.get("style") or ""):
            rank: _Rank = (2 if important else 1, 1, 1, (_TOP,), (0, 0, 0), 0)
            _offer(_Rule(_UNIVERSAL, prop, value, rank, False), YES, best, {})
        values = {prop: rule.value for prop, rule in best.items() if rule.value is not None}
        for prop in ("width", "height"):
            px = _px(values.get(prop, ""), self._viewport)
            if px is not None and px <= 1:
                return True
        if values.get("position") not in ("absolute", "fixed"):
            return False
        return any(
            _far(values.get(prop), self._viewport, sign)
            for prop, sign in (("left", 0), ("right", 0), ("top", -1), ("bottom", 1))
        )

    def _candidates(self, element: Element) -> Iterator[_Rule]:
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
        for prelude, body in _blocks(UTILITY_CSS):
            for text in _split_commas(prelude):
                selector, _, _ = _complex(text)
                if selector is None or _key(selector.parts[-1]) in self._by_key:
                    continue
                for prop, value, _important in _declaration(body or ""):
                    self._order += 1
                    rank: _Rank = (1, 1, 0, (-1,), selector.specificity, self._order)
                    self._index(_Rule(selector, prop, value, rank, True))

    def _sheet(self, text: str, context: _Context) -> None:
        if len(text) > MAX_SHEET_CHARS:
            self.unknown = True
        self._read(_COMMENT.sub(" ", text[:MAX_SHEET_CHARS]), [], context)

    def _read(self, text: str, parents: list[_Selector], context: _Context) -> None:
        for prelude, body in _blocks(text):
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
            for layer in _split_commas(condition):
                self._layer((*context.layer, *layer.split(".")))
        elif name == "import":
            self._import(condition, context)

    def _block(
        self, name: str, condition: str, body: str, parents: list[_Selector], context: _Context
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
            close = _closing(rest, len("supports("))
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

    def _selectors(self, prelude: str, parents: list[_Selector], body: str) -> list[_Selector]:
        out: list[_Selector] = []
        uncertain = False
        for text in _split_commas(prelude):
            if parents:
                for parent in parents:
                    nested = _nested(text, parent)
                    if nested is None:
                        nested = _fallback(text.replace("&", "*"))[0] or _UNIVERSAL
                        uncertain = True
                    out.append(nested)
                continue
            selector, validity, flag = _complex(text)
            if validity == _INVALID:
                return []
            uncertain = uncertain or validity == _UNKNOWN
            if flag == _GENERATED:
                if selector is not None and _generates_text(body):
                    self.generators.append(selector)
                continue
            if flag and any(True for _ in _declarations(body)):
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

    def _declare(self, selectors: list[_Selector], text: str, context: _Context) -> None:
        layer = self._layer_key(context.layer)
        normal, important_level = (0, 3) if self._user_agent else (1, 2)
        for prop, value, important in _declaration(text):
            for selector in selectors:
                self._order += 1
                level, key = (
                    (important_level, tuple(-k for k in layer)) if important else (normal, layer)
                )
                rank: _Rank = (level, 1, 0, key, selector.specificity, self._order)
                self._index(_Rule(selector, prop, value, rank, context.maybe))

    def _index(self, rule: _Rule) -> None:
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


_UNIVERSAL: Final = _Selector((_Compound(),), ())


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


def _offer(
    rule: _Rule, matched: int, best: dict[str, _Rule], maybe: dict[str, list[_Rule]]
) -> None:
    if matched == NO:
        return
    if matched == YES and not rule.maybe:
        current = best.get(rule.prop)
        if current is None or rule.rank > current.rank:
            best[rule.prop] = rule
    else:
        maybe.setdefault(rule.prop, []).append(rule)


@dataclass(frozen=True, slots=True)
class _Cascade:
    best: dict[str, _Rule]
    maybe: dict[str, list[_Rule]]
    present: frozenset[str]
    """The properties some rule sets."""

    def values(self, prop: str) -> tuple[str | None, ...]:
        """Every value `prop` may take: the certain winner's (or _UNSET), and those of the rules
        that may apply and would beat it."""
        winner = self.best.get(prop)
        found: list[str | None] = [_UNSET if winner is None else winner.value]
        for rule in self.maybe.get(prop, ()):
            if winner is None or rule.rank > winner.rank:
                found.append(rule.value)
        return tuple(found)

    def sets(self, *props: str) -> bool:
        return not self.present.isdisjoint(props)


_REMOVERS: Final = _words("display content-visibility")
_BOX: Final = _OBSCURERS | _words("clip position left right top bottom") | _MARGINS | _SIZES
_TEXT: Final = _TRACKED - _REMOVERS - _BOX - {"visibility"}


def _compute(cascade: _Cascade, parent: Computed, viewport: Viewport | None) -> Computed:
    present = cascade.present
    unsure = parent.unsure
    if not present.isdisjoint(_REMOVERS):
        shown = [_display_hides(v) for v in cascade.values("display")]
        contents = [_content_hides(v) for v in cascade.values("content-visibility")]
        if all(h == YES for h in shown) or all(h == YES for h in contents):
            return REMOVED
        unsure = unsure or any(h != NO for h in (*shown, *contents))
    visibility = parent.visibility
    if "visibility" in present:
        states = {_visibility(v, parent.visibility) for v in cascade.values("visibility")}
        visibility = states.pop() if len(states) == 1 else UNSURE
    if not unsure and not present.isdisjoint(_BOX):
        unsure = _obscured(cascade, viewport)
    if present.isdisjoint(_TEXT):
        if unsure == parent.unsure and visibility == parent.visibility:
            return parent
        return replace(parent, removed=False, unsure=unsure, visibility=visibility)
    font_px, font_tiny = parent.font_px, parent.font_tiny
    if cascade.sets("font-size"):
        sizes = [_font_px(v, parent.font_px, viewport) for v in cascade.values("font-size")]
        font_tiny = any(
            (px is not None and px < _TINY_FONT)
            or (px is None and v in (_UNSET, "inherit", "unset") and parent.font_tiny)
            for px, v in zip(sizes, cascade.values("font-size"), strict=True)
        )
        font_px = sizes[0] if len(set(sizes)) == 1 else None
    color, clear = parent.color, parent.clear
    if cascade.sets("color", "-webkit-text-fill-color"):
        color, clear = _paint(cascade, parent)
    background = parent.background
    if cascade.sets("background-color"):
        found = {_background(v, parent.background) for v in cascade.values("background-color")}
        background = found.pop() if len(found) == 1 else None
    clip_text = parent.clip_text or any(
        v is not None and "text" in v.split()
        for p in ("background-clip", "-webkit-background-clip")
        for v in cascade.values(p)
    )
    indent_far = parent.indent_far
    if cascade.sets("text-indent"):
        indent_far = any(
            parent.indent_far if v in (_UNSET, "inherit", "unset") else _indent_far(v, viewport)
            for v in cascade.values("text-indent")
        )
    return Computed(
        False,
        unsure,
        visibility,
        font_px,
        font_tiny,
        color,
        clear,
        background,
        clip_text,
        indent_far,
        _inherits(cascade, "fill", parent.fill_none, _fill_none),
        _inherits(cascade, "fill-opacity", parent.fill_faint, _faint),
    )


def _inherits(cascade: _Cascade, prop: str, inherited: bool, test: Callable[[str], bool]) -> bool:
    """Whether an inherited property may take a value `test` holds for."""
    if not cascade.sets(prop):
        return inherited
    return any(
        inherited if v in (_UNSET, "inherit", "unset") else v is None or test(v)
        for v in cascade.values(prop)
    )


def _fill_none(value: str) -> bool:
    if value in ("none", "transparent"):
        return True
    rgba = _color(value)
    return rgba is not None and rgba[3] <= _FAINT


def _faint(value: str) -> bool:
    amount = _amount(value)
    return amount is not None and amount <= _FAINT


def _display_hides(value: str | None) -> int:
    if value is None:
        return MAYBE
    if value == _UNSET:
        return NO
    if value == "none":
        return YES
    tokens = value.split()
    if tokens and all(t in _DISPLAY for t in tokens):
        return NO
    return MAYBE


def _content_hides(value: str | None) -> int:
    if value is None:
        return MAYBE
    if value == "hidden":
        return YES
    if value in (_UNSET, "visible", "auto", "inherit", "initial", "unset"):
        return NO
    return MAYBE


def _visibility(value: str | None, inherited: int) -> int:
    if value in (_UNSET, "inherit", "unset"):
        return inherited
    if value in ("visible", "initial", "revert"):
        return SHOWN
    if value in ("hidden", "collapse"):
        return HIDDEN
    return UNSURE


def _obscured(cascade: _Cascade, viewport: Viewport | None) -> bool:
    """Whether this element's box may be invisible or off-screen."""
    for prop in _OBSCURERS & cascade.present:
        if any(v is not None and _hides(prop, v) for v in cascade.values(prop)):
            return True
    positions = cascade.values("position") if cascade.sets("position") else (_UNSET,)
    placed = any(v in ("absolute", "fixed") for v in positions)
    moved = placed or any(v in ("relative", "sticky") for v in positions)
    if placed and any(v is not None and _clip_rect_hides(v) for v in cascade.values("clip")):
        return True
    if moved and cascade.sets("left", "right", "top", "bottom"):
        for prop, sign in (("left", 0), ("right", 0), ("top", -1), ("bottom", 1)):
            if any(_far(v, viewport, sign) for v in cascade.values(prop)):
                return True
    for prop in _MARGINS & cascade.present:
        if any(_far(v, viewport, -1) for v in cascade.values(prop)):
            return True
    if cascade.sets(*_SIZES) and cascade.sets("overflow-x", "overflow-y"):
        clipped = any(
            v in ("hidden", "clip") for p in ("overflow-x", "overflow-y") for v in cascade.values(p)
        )
        sizes = _SIZES & cascade.present
        if clipped and any(_tiny_size(v, viewport) for p in sizes for v in cascade.values(p)):
            return True
    return False


def _hides(prop: str, value: str) -> bool:
    if prop in ("opacity", "zoom"):
        amount = _amount(value)
        if prop == "zoom" and amount == 0:
            return False
        return amount is not None and amount <= _FAINT
    if prop == "filter":
        return any(
            name == "opacity" and (a := _amount(args)) is not None and a <= _FAINT
            for name, args in _FUNCTION.findall(value)
        )
    if prop == "scale":
        return any((a := _amount(t)) is not None and abs(a) <= _FAINT for t in value.split())
    if prop == "translate":
        return any(_far(t, None, 0) for t in value.split())
    if prop == "clip-path":
        return _clip_path_hides(value)
    return _transform_hides(value)


def _transform_hides(value: str) -> bool:
    for name, args in _FUNCTION.findall(value):
        values = [a.strip() for a in args.split(",")]
        if name in ("scale", "scalex", "scaley", "scale3d"):
            if any((a := _amount(v)) is not None and abs(a) <= _FAINT for v in values):
                return True
        elif name in ("translate", "translatex", "translatey", "translate3d"):
            if any(_far(v, None, 0) for v in values):
                return True
        elif name == "matrix" and len(values) == 6:
            numbers = [_amount(v) for v in values]
            if all(n is not None for n in numbers):
                a, b, c, d, e, f = (n or 0.0 for n in numbers)
                if abs(a * d - b * c) <= _FAINT**2 or max(abs(e), abs(f)) >= _FAR:
                    return True
        elif name in ("rotatex", "rotatey"):
            angle = _degrees(values[0])
            if angle is not None and abs((angle % 180) - 90) < 1:
                return True
    return False


def _clip_rect_hides(value: str) -> bool:
    found = _FUNCTION.search(value)
    if found is None or found.group(1) != "rect":
        return False
    sides = [s for s in re.split(r"[\s,]+", found.group(2).strip()) if s]
    return any(s != "auto" for s in sides)


def _clip_path_hides(value: str) -> bool:
    found = _FUNCTION.search(value)
    if found is None:
        return False
    name, args = found.group(1), found.group(2)
    if name == "inset":
        sides = args.split(" round ")[0].split()[:4]
        percents = [p for side in sides if (p := _percent(side)) is not None]
        if not sides or len(percents) != len(sides):
            return any(_far(side, None, 0) for side in sides)
        top, right, bottom, left = _box(percents)
        return top + bottom >= 100 or left + right >= 100
    if name in ("circle", "ellipse"):
        radii = args.split(" at ")[0].split()
        return bool(radii) and all(_amount(r) == 0 or _percent(r) == 0 for r in radii)
    if name == "polygon":
        points = {" ".join(p.split()) for p in args.split(",")}
        return len(points) == 1
    return False


def _box(values: list[float]) -> tuple[float, float, float, float]:
    if len(values) == 1:
        return values[0], values[0], values[0], values[0]
    if len(values) == 2:
        return values[0], values[1], values[0], values[1]
    if len(values) == 3:
        return values[0], values[1], values[2], values[1]
    return values[0], values[1], values[2], values[3]


def _paint(cascade: _Cascade, parent: Computed) -> tuple[Rgba | None, bool]:
    colors: set[Rgba | None] = set()
    clear = False
    for value in cascade.values("color"):
        if value in (_UNSET, "inherit", "unset"):
            colors.add(parent.color)
            clear = clear or parent.clear
            continue
        rgba = BLACK if value == "initial" else (None if value is None else _color(value))
        colors.add(rgba)
        clear = clear or (rgba is not None and rgba[3] <= _FAINT)
    for value in cascade.values("-webkit-text-fill-color"):
        if value is None or value in (_UNSET, "inherit", "unset", "currentcolor", "initial"):
            continue
        rgba = _color(value)
        clear = clear or (rgba is not None and rgba[3] <= _FAINT)
    color = colors.pop() if len(colors) == 1 else None
    return color, clear


def _background(value: str | None, behind: Rgba | None) -> Rgba | None:
    if value in (_UNSET, "transparent", "initial", "unset"):
        return behind
    if value is None or value == "inherit":
        return None
    rgba = _color(value)
    if rgba is None:
        return None
    if rgba[3] <= _FAINT:
        return behind
    return rgba if rgba[3] >= 1 else None


def _color(value: str) -> Rgba | None:
    value = value.strip()
    if value in _NAMED:
        return _NAMED[value]
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (3, 4):
            digits = "".join(d * 2 for d in digits)
        if len(digits) in (6, 8) and all(c in "0123456789abcdef" for c in digits):
            alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1.0
            return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16), alpha)
        return None
    found = _FUNCTION.fullmatch(value)
    if found is None or found.group(1) not in ("rgb", "rgba", "hsl", "hsla"):
        return None
    parts = [p for p in re.split(r"[\s,/]+", found.group(2).strip()) if p]
    if len(parts) not in (3, 4):
        return None
    alpha = 1.0
    if len(parts) == 4:
        given = _percent(parts[3])
        alpha_value = given / 100 if given is not None else _amount(parts[3])
        if alpha_value is None:
            return None
        alpha = alpha_value
    if found.group(1).startswith("hsl"):
        hue = _degrees(parts[0]) if not parts[0].replace(".", "").isdigit() else _amount(parts[0])
        saturation, lightness = _percent(parts[1]), _percent(parts[2])
        if hue is None or saturation is None or lightness is None:
            return None
        return (*_hsl(hue, saturation / 100, lightness / 100), alpha)
    channels: list[float] = []
    for part in parts[:3]:
        share = _percent(part)
        amount = share * 2.55 if share is not None else _amount(part)
        if amount is None:
            return None
        channels.append(round(amount))
    return (channels[0], channels[1], channels[2], alpha)


def _hsl(hue: float, saturation: float, lightness: float) -> tuple[float, float, float]:
    def channel(n: float) -> float:
        k = (n + hue / 30) % 12
        a = saturation * min(lightness, 1 - lightness)
        return round(255 * (lightness - a * max(-1, min(k - 3, 9 - k, 1))))

    return channel(0), channel(8), channel(4)


def _number(value: str) -> tuple[float, str] | None:
    found = _NUMBER.match(value.strip())
    if found is None:
        return None
    return float(found.group(1)), found.group(2)


def _amount(value: str) -> float | None:
    """A plain number, or a percentage as a fraction."""
    found = _number(value)
    if found is None:
        return None
    number, unit = found
    if unit == "%":
        return number / 100
    return number if not unit else None


def _percent(value: str) -> float | None:
    found = _number(value)
    if found is None:
        return None
    number, unit = found
    if unit == "%" or (number == 0 and not unit):
        return number
    return None


def _degrees(value: str) -> float | None:
    found = _number(value)
    if found is None:
        return None
    number, unit = found
    return {
        "deg": number,
        "turn": number * 360,
        "rad": number * 57.29578,
        "grad": number * 0.9,
    }.get(unit)


def _px(value: str, viewport: Viewport | None, font: float | None = 16.0) -> float | None:
    """A length in CSS pixels, or None when it is relative to something unknown."""
    found = _number(value)
    if found is None:
        return None
    number, unit = found
    if not unit:
        return number if number == 0 else None
    if unit in _UNITS:
        return number * _UNITS[unit]
    if unit in ("em", "ex", "ch") and font is not None:
        return number * font * (1 if unit == "em" else 0.5)
    if unit in ("vw", "vh", "vmin", "vmax") and viewport is not None:
        width, height = viewport
        side = {"vw": width, "vh": height, "vmin": min(viewport), "vmax": max(viewport)}[unit]
        return number * side / 100
    return None


def _far(value: str | None, viewport: Viewport | None, sign: int) -> bool:
    """Whether an offset of `value` moves a box off-screen: `sign` -1 only counts large negative
    offsets, 1 large positive ones, 0 either."""
    if value is None or value == _UNSET:
        return False
    px = _px(value, viewport)
    if px is None:
        return False
    return px <= -_FAR if sign < 0 else (px >= _FAR if sign > 0 else abs(px) >= _FAR)


def _tiny_size(value: str | None, viewport: Viewport | None) -> bool:
    if value is None or value == _UNSET:
        return False
    if _percent(value) == 0:
        return True
    px = _px(value, viewport)
    return px is not None and px <= 1


def _indent_far(value: str | None, viewport: Viewport | None) -> bool:
    if value is None:
        return False
    share = _percent(value.split()[0] if value.split() else value)
    if share is not None:
        return share <= -100
    return _far(value.split()[0] if value.split() else value, viewport, 0)


_FONT_KEYWORDS: Final = {
    "xx-small": 9.0,
    "x-small": 10.0,
    "small": 13.0,
    "medium": 16.0,
    "large": 18.0,
    "x-large": 24.0,
    "xx-large": 32.0,
    "xxx-large": 48.0,
    "initial": 16.0,
}


def _font_px(value: str | None, inherited: float | None, viewport: Viewport | None) -> float | None:
    if value is None:
        return None
    if value in (_UNSET, "inherit", "unset"):
        return inherited
    if value in _FONT_KEYWORDS:
        return _FONT_KEYWORDS[value]
    if value in ("smaller", "larger"):
        return None if inherited is None else inherited * (1.2 if value == "larger" else 1 / 1.2)
    if (share := _percent(value)) is not None:
        return None if inherited is None else inherited * share / 100
    return _px(value, viewport, inherited)


def _blocks(text: str) -> Iterator[tuple[str, str | None]]:
    """The items of one level of `text`: ``(prelude, body)`` for a ``prelude { body }`` block,
    ``(declaration, None)`` for text that ends with ``;`` or the end of the level."""
    start, depth, at, quote = 0, 0, 0, ""
    opened = 0
    while (match := _SPECIAL.search(text, at)) is not None:
        char, at = match.group(), match.end()
        if quote:
            if char == "\\":
                at += 1
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == "\\":
            at += 1
        elif char == "{":
            if depth == 0:
                opened = at
            depth += 1
        elif char == "}":
            if depth == 0:
                start = at
                continue
            depth -= 1
            if depth == 0:
                yield text[start : opened - 1].strip(), text[opened : at - 1]
                start = at
        elif char == ";" and depth == 0:
            yield text[start : at - 1].strip(), None
            start = at
    if depth == 0 and text[start:].strip():
        yield text[start:].strip(), None
    elif depth > 0:
        yield text[start : opened - 1].strip(), text[opened:]


_GENERATED: Final = "generated"
_CONTENT = re.compile(r"(?:^|[;{\s])content\s*:\s*([^;}]*)", re.IGNORECASE)
_QUOTED = re.compile(r"\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'")


def _generates_text(body: str) -> bool:
    """A declaration list sets ``content`` to text: an ``attr()``, a counter, or a string with
    a letter or digit (an icon font's private-use character and ``""`` are not text)."""
    for match in _CONTENT.finditer(body):
        value = match.group(1)
        if re.search(r"\b(?:attr|counters?)\(", value, re.IGNORECASE):
            return True
        for quoted in _QUOTED.finditer(value):
            if any(c.isalnum() for c in _unescape(quoted.group(1) or quoted.group(2) or "")):
                return True
    return False


def _declarations(text: str) -> Iterator[tuple[str, str | None, bool]]:
    """The tracked properties a declaration list sets: (property, value, important)."""
    for declaration, body in _blocks(text):
        if body is None:
            yield from _declaration(declaration)


def _declaration(text: str) -> Iterator[tuple[str, str | None, bool]]:
    prop, colon, raw = text.partition(":")
    prop = prop.strip().lower()
    if not colon or (prop not in _TRACKED and prop not in _SHORTHANDS):
        return
    value = " ".join(raw.lower().split())
    important = re.search(r"!\s*important$", value)
    if important is not None:
        value = value[: important.start()].strip()
    if "\\" in value:
        value = " ".join(_unescape(value).split())
    if not value:
        return
    readable = not any(f in value for f in ("var(", "env(", "attr("))
    flag = important is not None
    for name, found in _expand(prop, value):
        yield name, (found if readable else None), flag


def _expand(prop: str, value: str) -> Iterator[tuple[str, str]]:
    """The tracked longhands a declaration sets."""
    match prop:
        case "all":
            if value in ("initial", "unset", "inherit"):
                yield "display", "inline" if value != "inherit" else "inherit"
                yield "visibility", "visible" if value == "initial" else "inherit"
            else:
                yield "display", "revert"
                yield "visibility", "revert"
        case "font":
            yield "font-size", _font_size(value)
        case "background":
            if "url(" in value or "gradient(" in value or "image(" in value:
                yield "background-color", "image"
            else:
                yield (
                    "background-color",
                    next((t for t in _tokens(value) if _color(t) is not None), "transparent"),
                )
            if "text" in value.split():
                yield "background-clip", "text"
        case "inset":
            top, right, bottom, left = _sides(value)
            yield from (("top", top), ("right", right), ("bottom", bottom), ("left", left))
        case "margin":
            top, _, _, left = _sides(value)
            yield from (("margin-top", top), ("margin-left", left))
        case "overflow":
            first, *rest = value.split() or [""]
            yield from (("overflow-x", first), ("overflow-y", rest[0] if rest else first))
        case _:
            yield prop, value


def _tokens(value: str) -> list[str]:
    """Space-separated tokens, keeping functions whole."""
    out: list[str] = []
    depth, start = 0, 0
    for i, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == " " and depth == 0:
            out.append(value[start:i])
            start = i + 1
    out.append(value[start:])
    return [t for t in out if t]


def _sides(value: str) -> tuple[str, str, str, str]:
    tokens = _tokens(value) or ["0"]
    values = [*tokens[:4]]
    if len(values) == 1:
        return values[0], values[0], values[0], values[0]
    if len(values) == 2:
        return values[0], values[1], values[0], values[1]
    if len(values) == 3:
        return values[0], values[1], values[2], values[1]
    return values[0], values[1], values[2], values[3]


def _font_size(value: str) -> str:
    """The size a ``font`` shorthand sets ("medium" for a system font keyword, "unknown" when it
    cannot be read)."""
    if value in ("inherit", "initial", "unset"):
        return value
    for token in _tokens(value):
        size = token.split("/", 1)[0]
        if size in _FONT_KEYWORDS or size in ("smaller", "larger"):
            return size
        if _number(size) is not None and (_number(size) or (0, ""))[1]:
            return size
        if size == "0":
            return size
    return "medium" if len(_tokens(value)) == 1 else "unknown"


def _split_commas(text: str) -> list[str]:
    parts: list[str] = []
    depth, start = 0, 0
    for i, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return [p for p in parts if p]


def _nested(text: str, parent: _Selector) -> _Selector | None:
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
    own, validity, _ = _complex(f"*{tail}")
    if own is None or validity != _VALID:
        return None
    maybe = parent.maybe or own.maybe
    if joined:
        merged = _merge(parent.parts[-1], own.parts[0])
        return _Selector(
            (*parent.parts[:-1], merged, *own.parts[1:]),
            (*parent.combinators, *own.combinators),
            maybe,
        )
    return _Selector(
        (*parent.parts, *own.parts[1:]), (*parent.combinators, *own.combinators), maybe
    )


def _merge(a: _Compound, b: _Compound) -> _Compound:
    return _Compound(
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
    pseudos: list[_Pseudo] = field(default_factory=list[_Pseudo])
    host: bool = False
    host_args: list[_Compound] = field(default_factory=list[_Compound])
    host_maybe: bool = False
    element: str | None = None
    """The pseudo-element the compound ends with."""

    def build(self) -> _Compound:
        return _Compound(
            self.tag,
            tuple(self.ids),
            tuple(self.classes),
            tuple(self.attrs),
            tuple(self.pseudos),
            self.host,
            tuple(self.host_args),
            self.host_maybe,
        )


def _complex(text: str) -> tuple[_Selector | None, int, str]:
    """A complex selector, how sure the reader is that a browser accepts it (_VALID, _UNKNOWN or
    _INVALID), and the scope flag of a ``::part()``, ``::slotted()`` or ``::details-content``
    rule. The selector is None when it styles a pseudo-element instead of an element; when the
    reader cannot parse it, it is an approximation that may match (its last compound)."""
    text = text.strip()
    parts: list[_Compound] = []
    combinators: list[str] = []
    found = _Builder()
    pending: str | None = None
    validity, maybe, flag = _VALID, False, ""
    seen = False
    at = 0
    while at < len(text):
        match = _TOKEN.match(text, at)
        if match is None:
            return _fallback(text)
        at = match.end()
        groups = match.groupdict()
        if groups["space"] is not None:
            pending = pending or " "
            continue
        if groups["comb"] is not None:
            if pending not in (None, " "):
                return None, _INVALID, ""
            pending = groups["comb"]
            continue
        if pending is not None:
            if not seen or found.element is not None:
                return None, _INVALID, ""
            parts.append(found.build())
            combinators.append(pending)
            found, pending = _Builder(), None
        seen = True
        if groups["star"] is not None:
            continue
        if groups["tag"] is not None:
            found.tag = _unescape(groups["tag"]).upper()
        elif groups["id"] is not None:
            found.ids.append(_unescape(groups["id"]))
        elif groups["cls"] is not None:
            found.classes.append(_unescape(groups["cls"]))
        elif groups["attr"] is not None:
            value = groups["dq"] if groups["dq"] is not None else groups["sq"]
            if value is None and groups["bare"] is not None:
                value = _unescape(groups["bare"])
            folded = (groups["flag"] or "").lower() == "i"
            found.attrs.append(
                (_unescape(groups["attr"]).lower(), groups["op"], value or "", folded)
            )
        else:
            raw = groups["pseudo"].lower()
            name = raw.lstrip(":")
            args: str | None = None
            if groups["args"] is not None:
                close = _closing(text, at)
                if close is None:
                    return _fallback(text)
                args, at = text[at:close], close + 1
            element = raw.startswith("::") or name in _LEGACY_PSEUDO_ELEMENTS
            if element:
                if found.element is not None:
                    return None, _INVALID, ""
                found.element = name
                if name in ("part", "slotted", "details-content"):
                    flag = {"details-content": "details"}.get(name, name)
                elif name not in _PSEUDO_ELEMENTS:
                    validity = max(validity, _INVALID if _foreign(name) else _UNKNOWN)
                continue
            outcome = _pseudo_class(name, args, found)
            if outcome == _INVALID:
                return None, _INVALID, ""
            if outcome == _UNKNOWN:
                validity, maybe = max(validity, _UNKNOWN), True
    if not seen or pending not in (None, " "):
        return None, _INVALID, ""
    parts.append(found.build())
    if found.element is not None:
        if found.element in ("first-line", "first-letter"):
            return _Selector(tuple(parts), tuple(combinators), True), validity, flag
        if found.element in ("before", "after"):
            return _Selector(tuple(parts), tuple(combinators), maybe), validity, _GENERATED
        return None, validity, flag
    return _Selector(tuple(parts), tuple(combinators), maybe), validity, flag


_STRUCTURAL: Final = _words(
    "first-child last-child only-child first-of-type last-of-type only-of-type defined any-link"
)


def _foreign(name: str) -> bool:
    """A vendor prefix Chromium rejects."""
    return name.startswith(("-moz-", "-ms-", "-o-"))


def _pseudo_class(name: str, args: str | None, found: _Builder) -> int:
    """Add pseudo-class `name` (with `args`) to `found`; how sure the reader is it is valid."""
    if args is None:
        if name in ("root", "scope"):
            found.pseudos.append(_Pseudo("root"))
        elif name in _STRUCTURAL:
            found.pseudos.append(_Pseudo(name))
        elif name == "host":
            found.host = True
        elif name in _STATE_PSEUDOS:
            found.pseudos.append(_Pseudo("maybe"))
        elif _foreign(name):
            return _INVALID
        else:
            found.pseudos.append(_Pseudo("maybe"))
            return _UNKNOWN
        return _VALID
    if name in ("not", "is", "where", "matches", "-webkit-any"):
        forgiving = name != "not"
        selectors: list[_Selector] = []
        unsure = False
        for text in _split_commas(args):
            selector, validity, _ = _complex(text)
            if validity == _INVALID:
                if not forgiving:
                    return _INVALID
                continue
            unsure = unsure or validity == _UNKNOWN
            if selector is not None:
                selectors.append(selector)
        kind = "not" if name == "not" else ("where" if name == "where" else "is")
        found.pseudos.append(_Pseudo(kind, tuple(selectors), maybe=unsure))
        return _UNKNOWN if unsure and not forgiving else _VALID
    if name in ("nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type"):
        formula, _, of = args.partition(" of ")
        nth = _nth(formula)
        if nth is None:
            return _INVALID
        found.pseudos.append(_Pseudo("maybe") if of.strip() else _Pseudo(name, nth=nth))
        return _VALID
    if name == "host":
        found.host = True
        compound, validity, _ = _complex(args)
        if compound is None or len(compound.parts) != 1 or validity != _VALID:
            found.host_maybe = True
        else:
            found.host_args.append(compound.parts[0])
        return _VALID
    if name == "host-context":
        found.host, found.host_maybe = True, True
        return _VALID
    if name in ("has", "lang", "dir", "state", "active-view-transition-type"):
        found.pseudos.append(_Pseudo("maybe"))
        return _VALID
    if _foreign(name):
        return _INVALID
    found.pseudos.append(_Pseudo("maybe"))
    return _UNKNOWN


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


def _fallback(text: str) -> tuple[_Selector | None, int, str]:
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
        selector, validity, _ = _complex(tail)
        if selector is not None and validity != _INVALID and len(selector.parts) == 1:
            return replace(selector, maybe=True), _UNKNOWN, ""
    return _Selector((_Compound(),), (), True), _UNKNOWN, ""


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


def _key(part: _Compound) -> str:
    """The key a compound is indexed by: ``:host``, its id, else its last class (a modifier is
    rarer than the base class it modifies), else its tag."""
    if part.host:
        return ":host"
    if part.ids:
        return f"#{part.ids[0]}"
    if part.classes:
        return f".{part.classes[-1]}"
    return part.tag or "*"


def _closing(text: str, at: int) -> int | None:
    depth = 1
    for i in range(at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _unescape(text: str) -> str:
    def replace_escape(match: re.Match[str]) -> str:
        code = match.group(1)
        if code[0] not in "0123456789abcdefABCDEF":
            return code
        point = int(code.strip(), 16)
        return chr(point) if 0 < point <= 0x10FFFF else "�"

    return _ESCAPE.sub(replace_escape, text)


def _matches(selector: _Selector, element: Element) -> int:
    found = _match(selector, len(selector.parts) - 1, element)
    return min(found, MAYBE) if selector.maybe else found


def _match(selector: _Selector, index: int, element: Element) -> int:
    found = _compound_match(selector.parts[index], element)
    if found == NO or index == 0:
        return found
    combinator = selector.combinators[index - 1]
    if combinator == ">":
        parent = element.parent
        return NO if parent is None else min(found, _match(selector, index - 1, parent))
    if combinator == "+":
        previous = element.previous
        return NO if previous is None else min(found, _match(selector, index - 1, previous))
    best = NO
    other = element.parent if combinator == " " else element.previous
    while other is not None:
        matched = _match(selector, index - 1, other)
        if matched == YES:
            return found
        best = max(best, matched)
        other = other.parent if combinator == " " else other.previous
    return min(found, best)


def _compound_match(part: _Compound, element: Element) -> int:
    if element.host is not None:
        if not part.host:
            return NO
        if part.host_maybe:
            return MAYBE
        return min((_compound_match(arg, element.host) for arg in part.host_args), default=YES)
    if part.host:
        return NO
    if part.tag is not None and part.tag != element.tag:
        return NO
    if any(i != element.attrs.get("id") for i in part.ids):
        return NO
    if any(c not in element.classes for c in part.classes):
        return NO
    if not all(_attr_matches(a, element.attrs) for a in part.attrs):
        return NO
    found = YES
    for pseudo in part.pseudos:
        found = min(found, _pseudo_match(pseudo, element))
        if found == NO:
            return NO
    return found


def _pseudo_match(pseudo: _Pseudo, element: Element) -> int:
    match pseudo.name:
        case "root":
            return YES if element.tag == "HTML" and element.parent is None else NO
        case "not" | "is" | "where":
            found = max((_matches(s, element) for s in pseudo.args), default=NO)
            if pseudo.maybe:
                found = max(found, MAYBE)
            if pseudo.name == "not":
                return {NO: YES, MAYBE: MAYBE, YES: NO}[found]
            return found
        case "defined":
            return MAYBE if "-" in element.tag else YES
        case "any-link":
            return YES if element.tag in ("A", "AREA") and "href" in element.attrs else NO
        case "maybe":
            return MAYBE
        case _:
            return _structural(pseudo, element)


def _structural(pseudo: _Pseudo, element: Element) -> int:
    siblings = element.siblings or [element]
    index = element.index if element.siblings is not None else 0
    typed = pseudo.name.endswith("of-type")
    pool = [s for s in siblings if s.tag == element.tag] if typed else siblings
    position = pool.index(element) if typed else index
    count = len(pool)
    name = pseudo.name.removesuffix("-of-type").removesuffix("-child")
    if name == "first":
        return YES if position == 0 else NO
    if name == "last":
        return YES if position == count - 1 else NO
    if name == "only":
        return YES if count == 1 else NO
    step, offset = pseudo.nth or (0, 1)
    place = count - position if "last" in name else position + 1
    if step == 0:
        return YES if place == offset else NO
    steps, rest = divmod(place - offset, step)
    return YES if rest == 0 and steps >= 0 else NO


def _attr_matches(test: tuple[str, str | None, str, bool], attrs: dict[str, str]) -> bool:
    name, op, wanted, folded = test
    actual = next((v for k, v in attrs.items() if k.lower() == name), None)
    if actual is None:
        return False
    if op is None:
        return True
    if folded:
        actual, wanted = actual.lower(), wanted.lower()
    match op:
        case "=":
            return actual == wanted
        case "~=":
            return wanted in actual.split()
        case "|=":
            return actual == wanted or actual.startswith(f"{wanted}-")
        case "^=":
            return bool(wanted) and actual.startswith(wanted)
        case "$=":
            return bool(wanted) and actual.endswith(wanted)
        case _:
            return bool(wanted) and wanted in actual


def supports(condition: str) -> bool | None:
    """Whether an ``@supports`` condition holds in Chromium; None when the reader does not
    know."""
    text = condition.strip()
    lowered = text.lower()
    if lowered.startswith("not ") or lowered.startswith("not("):
        inner = supports(text[3:])
        return None if inner is None else not inner
    for joiner in (" or ", " and "):
        pieces = _split_top(text, joiner)
        if len(pieces) > 1:
            results = [supports(p) for p in pieces]
            if joiner == " or ":
                return True if True in results else (None if None in results else False)
            return False if False in results else (None if None in results else True)
    if text.startswith("(") and _closing(text, 1) == len(text) - 1:
        inner = text[1:-1].strip()
        prop, colon, value = inner.partition(":")
        if not colon or "(" in prop:
            return supports(inner)
        return _supported(prop.strip().lower(), " ".join(value.lower().split()))
    return None


def _supported(prop: str, value: str) -> bool | None:
    if _foreign(prop) or _foreign(value):
        return False
    if prop == "display":
        tokens = value.split()
        return True if tokens and all(t in _DISPLAY for t in tokens) else None
    return None


def _split_top(text: str, joiner: str) -> list[str]:
    pieces: list[str] = []
    depth, start, i = 0, 0, 0
    lowered = text.lower()
    while i < len(text):
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and lowered.startswith(joiner, i):
            pieces.append(text[start:i])
            i += len(joiner)
            start = i
            continue
        i += 1
    pieces.append(text[start:])
    return pieces


def media_holds(query: str, viewport: Viewport | None) -> bool | None:
    """Whether a media query list holds on a screen of `viewport` (CSS pixels, light colour
    scheme, a fine pointer that hovers); None when the reader cannot tell."""
    results = [_media_query(q, viewport) for q in _split_commas(query.lower())] or [True]
    if True in results:
        return True
    return None if None in results else False


def _media_query(query: str, viewport: Viewport | None) -> bool | None:
    negate = query.startswith("not ")
    query = query.removeprefix("not ").removeprefix("only ").strip()
    result: bool | None = True
    for term in re.split(r"\s+and\s+", query):
        term = term.strip()
        value = (
            _media_feature(term[1:-1].strip(), viewport)
            if term.startswith("(")
            else ({"all": True, "screen": True}.get(term, False))
        )
        if value is False:
            result = False
        elif value is None and result is not False:
            result = None
    if result is None:
        return None
    return (not result) if negate else result


_FEATURES: Final = {
    "prefers-color-scheme": {"light": True, "dark": False},
    "prefers-reduced-motion": {"no-preference": True, "reduce": False},
    "prefers-contrast": {"no-preference": True, "more": False, "less": False, "custom": False},
    "forced-colors": {"none": True, "active": False},
    "inverted-colors": {"none": True, "inverted": False},
    "scripting": {"enabled": True, "none": False, "initial-only": False},
    "hover": {"hover": True, "none": False},
    "any-hover": {"hover": True, "none": False},
    "pointer": {"fine": True, "coarse": False, "none": False},
    "any-pointer": {"fine": True, "coarse": False, "none": False},
    "display-mode": {"browser": True},
    "update": {"fast": True, "slow": False, "none": False},
}


def _media_feature(feature: str, viewport: Viewport | None) -> bool | None:
    ranged = _RANGE.match(feature)
    if ranged is not None and (ranged.group(1) or ranged.group(5)):
        low, low_op, dimension, high_op, high = ranged.groups()
        checks = [(_FLIPPED[low_op], low)] if low else []
        checks += [(high_op, high)] if high else []
        results = [_compare(dimension, op, length, viewport) for op, length in checks]
        return None if None in results else all(results)
    name, _, value = (p.strip() for p in feature.partition(":"))
    for prefix, op in (("min-", ">="), ("max-", "<="), ("", "=")):
        dimension = name.removeprefix(prefix)
        if name.startswith(prefix) and dimension in ("width", "height") and value:
            return _compare(dimension, op, value, viewport)
    if name == "orientation":
        if viewport is None:
            return None
        return value == ("portrait" if viewport[1] >= viewport[0] else "landscape")
    if name == "color" and not value:
        return True
    if name in ("monochrome", "grid") and not value:
        return False
    choices = _FEATURES.get(name)
    if choices is None:
        return None
    return choices.get(value, None) if value else True


def _compare(dimension: str, op: str, length: str, viewport: Viewport | None) -> bool | None:
    pixels = _px(length.strip(), None)
    if viewport is None or pixels is None:
        return None
    actual = viewport[0] if dimension == "width" else viewport[1]
    match op:
        case ">=":
            return actual >= pixels
        case "<=":
            return actual <= pixels
        case ">":
            return actual > pixels
        case "<":
            return actual < pixels
        case _:
            return actual == pixels
