"""The CSS reader's vocabulary: match and visibility answers, selectors, rules, elements and
what the cascade computes for them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import Final

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


Viewport = tuple[int, int]


Rgba = tuple[float, float, float, float]


UNSET: Final = ""
"""A property no rule sets."""


def words(text: str) -> frozenset[str]:
    return frozenset(text.split())


DISPLAY: Final = words(
    "block inline inline-block flex inline-flex grid inline-grid flow-root flow contents "
    "list-item table inline-table table-row table-cell table-row-group table-header-group "
    "table-footer-group table-column table-column-group table-caption ruby ruby-text "
    "ruby-base math run-in -webkit-box -webkit-inline-box inherit initial unset"
)


NO_CUSTOM: Final[Mapping[str, tuple[str, ...] | None]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Registration:
    """An ``@property`` rule: whether the custom property inherits, and its initial value
    (None: undefined)."""

    inherits: bool
    initial: str | None


Registered = Mapping[str, Registration | None]
"""The registered custom properties, by name; None for one whose registration the reader cannot
read."""


NOT_REGISTERED: Final[Registered] = MappingProxyType({})


BLACK: Final[Rgba] = (0, 0, 0, 1)


WHITE: Final[Rgba] = (255, 255, 255, 1)


type Rank = tuple[int, int, int, tuple[int, ...], tuple[int, int, int], int]
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
class Pseudo:
    name: str
    args: tuple[Selector, ...] = ()
    nth: tuple[int, int] | None = None
    maybe: bool = False
    """An argument could not be read: the pseudo-class may match where it says it does not."""
    state: str = ""
    """What a pseudo-class the reader cannot evaluate says (``hover``, ``has(.x)``): rules that
    name the same one apply in the same state."""

    @property
    def specificity(self) -> tuple[int, int, int]:
        if self.name == "where":
            return (0, 0, 0)
        if self.name in ("not", "is"):
            return max((a.specificity for a in self.args), default=(0, 0, 0))
        return (0, 1, 0)


@dataclass(frozen=True, slots=True)
class Compound:
    tag: str | None = None
    ids: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()
    attrs: tuple[tuple[str, str | None, str, bool], ...] = ()
    pseudos: tuple[Pseudo, ...] = ()
    host: bool = False
    """``:host``: matches only the shadow host, from inside its shadow tree."""
    host_args: tuple[Compound, ...] = ()
    host_maybe: bool = False

    @property
    def keys(self) -> frozenset[str]:
        """The keys (`Element.keys`) every element it matches has."""
        found = {f".{c}" for c in self.classes} | {f"#{i}" for i in self.ids}
        if self.tag is not None:
            found.add(self.tag)
        return frozenset(found)

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
class Selector:
    parts: tuple[Compound, ...]
    """Compound selectors from left to right."""
    combinators: tuple[str, ...]
    """Between consecutive parts: " " (descendant), ">", "+" or "~"."""
    maybe: bool = False
    """The reader approximated the selector: where it matches, the real one may not."""
    ancestors: frozenset[str] = field(init=False, compare=False, repr=False)
    """The keys (`Element.keys`) an ancestor of every element it matches has: those of each
    compound before a descendant or child combinator."""

    def __post_init__(self) -> None:
        keys: set[str] = set()
        for part, combinator in zip(self.parts, self.combinators, strict=False):
            if combinator in " >" and not part.host:
                keys.update(part.keys)
        object.__setattr__(self, "ancestors", frozenset(keys))

    @property
    def specificity(self) -> tuple[int, int, int]:
        total = (0, 0, 0)
        for part in self.parts:
            a, b, c = part.specificity
            total = (total[0] + a, total[1] + b, total[2] + c)
        return total


@dataclass(frozen=True, slots=True)
class Rule:
    selector: Selector
    prop: str
    value: str | None
    """None: a value the reader cannot read (``var()``, ...)."""
    rank: Rank
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
    matched: dict[tuple[int, int], tuple[Selector, int]] | None = field(init=False, default=None)
    """What `matching` has worked out for this element, by selector and compound."""

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


MIN_CONTRAST: Final = 1.2
"""A WCAG contrast ratio below which text barely stands out from its background."""


FAINT_ALPHA: Final = 0.05
"""An opacity at or below which the reader is unsure text can be seen."""


def linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def over(colour: Rgba, behind: Rgba) -> Rgba:
    """`colour` painted over the opaque `behind`."""
    alpha = colour[3]
    r, g, b = (c * alpha + d * (1 - alpha) for c, d in zip(colour[:3], behind[:3], strict=True))
    return (r, g, b, 1.0)


def luminance(colour: Rgba) -> float:
    r, g, b = (linear(c / 255) for c in colour[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(colour: Rgba, behind: Rgba) -> float:
    """The WCAG contrast ratio of `colour` painted over the opaque `behind`."""
    one, two = luminance(over(colour, behind)), luminance(behind)
    return (max(one, two) + 0.05) / (min(one, two) + 0.05)


Box = tuple[Element, int | None, str]
"""A positioned element, its ``z-index`` (0 for ``auto``, None when the reader cannot read it)
and its ``position``."""


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
    """The smallest font size it may have, None when not known."""
    font_tiny: bool = False
    colors: tuple[Rgba, ...] = (BLACK,)
    """The colours its ``color`` may be."""
    clear: bool = False
    """Its text colour (or text fill) may be transparent, or one the reader cannot read."""
    paints: tuple[Rgba, ...] | None = None
    """The colours its ``-webkit-text-fill-color`` may be (None: its ``color``)."""
    background: tuple[Rgba, ...] | None = (WHITE,)
    """The opaque colours behind it (more than one under a gradient), None when not known."""
    clip_text: bool = False
    """A background shows through its text (``background-clip: text``)."""
    indent_far: bool = False
    fill_none: bool = False
    """Its SVG ``fill`` may paint nothing (``none``, a transparent colour)."""
    fill_faint: bool = False
    """Its SVG ``fill-opacity`` may be near 0."""
    fills: tuple[Rgba, ...] = (BLACK,)
    """The colours its SVG ``fill`` may be."""
    alpha: float = 1.0
    """The least its own and its ancestors' ``opacity`` multiply to."""
    flipped: bool = False
    """It or an ancestor may be turned in 3D to show its back."""
    deep: bool = False
    """An ancestor sets ``perspective``: a 3D transform of its own is one the reader does not
    measure."""
    boxes: tuple[Box, ...] = field(default=(), compare=False)
    """It and its positioned ancestors, outermost first."""
    svg: bool = False
    """It is an SVG element, whose text its ``fill`` paints."""
    custom: Mapping[str, tuple[str, ...] | None] = field(default=NO_CUSTOM, compare=False)
    """The custom properties its children inherit (`variables.Custom`)."""
    registered: Registered = field(default=NOT_REGISTERED, compare=False)
    """The custom properties ``@property`` rules register."""

    @property
    def text(self) -> int:
        """Whether this element's own text shows: SHOWN, UNSURE or HIDDEN."""
        if self.visibility == HIDDEN:
            return HIDDEN
        if self.svg:
            faded = self.fill_none or self.fill_faint or self._faint(self.fills)
        else:
            faded = not self.clip_text and (
                self.clear or self._faint(self.colors if self.paints is None else self.paints)
            )
        if (
            self.unsure
            or self.visibility == UNSURE
            or self.font_tiny
            or self.indent_far
            or faded
            or self.alpha <= FAINT_ALPHA
        ):
            return UNSURE
        return SHOWN

    def _faint(self, paints: tuple[Rgba, ...]) -> bool:
        """Whether a colour text may be painted in barely stands out from the background."""
        behind = self.background
        if behind is None:
            return False
        return faint(paints, self.alpha, behind)


@lru_cache(maxsize=4096)
def faint(paints: tuple[Rgba, ...], alpha: float, behind: tuple[Rgba, ...]) -> bool:
    """Whether a paint barely stands out from any of the colours behind it: under a gradient,
    text faint against one stop may be hidden there."""
    return any(
        contrast((r, g, b, a * alpha), back) < MIN_CONTRAST
        for r, g, b, a in paints
        for back in behind
    )


REMOVED: Final = Computed(removed=True)


ROOT: Final = Computed()
