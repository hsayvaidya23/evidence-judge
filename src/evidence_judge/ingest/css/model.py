"""The CSS reader's vocabulary: match and visibility answers, selectors, rules, elements and
what the cascade computes for them."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    matched: dict[tuple[int, int], tuple[Selector, int]] = field(
        init=False, default_factory=dict[tuple[int, int], tuple["Selector", int]]
    )
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
