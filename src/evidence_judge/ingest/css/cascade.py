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

from evidence_judge.ingest.css.animations import (
    Keyframes,
    fills_forwards,
    read_keyframes,
    resting,
    settles_shown,
)
from evidence_judge.ingest.css.backgrounds import UnreadableError, background_images
from evidence_judge.ingest.css.colors import parse_color
from evidence_judge.ingest.css.conditions import media_holds, supports
from evidence_judge.ingest.css.matching import matches
from evidence_judge.ingest.css.model import (
    BLACK,
    FAINT_ALPHA,
    HIDDEN,
    HTML,
    MATHML,
    MAYBE,
    NO,
    SVG,
    YES,
    Compound,
    Computed,
    Element,
    Rank,
    Registered,
    Registration,
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
    SHORTHANDS,
    blocks,
    declarations,
    expand,
    generates_text,
    parse_declaration,
    split_commas,
    unescape,
)
from evidence_judge.ingest.css.values import (
    MAX_NESTING,
    closing_paren,
    far_offset,
    functions,
    length_px,
    parse_number,
    parse_percent,
)
from evidence_judge.ingest.css.variables import (
    Custom,
    custom_properties,
    references,
    resolve_rule,
)
from evidence_judge.ingest.css.visibility import Cascade, compute, stowed

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


_PRESENTATION: Final = words(
    "display visibility opacity fill fill-opacity font-size transform clip-path mask filter color"
)
"""SVG presentation attributes the reader applies: author-level, beneath every rule."""


_SVG_UNITS: Final = {"translate": "px", "rotate": "deg", "skewx": "deg", "skewy": "deg"}
"""The unit an SVG ``transform`` attribute's bare numbers are in, by function."""


_SKIPPED_AT_RULES: Final = words(
    "font-face page counter-style "
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
    """Where a rule is read: under what uncertain condition, in what layer, from what URL, how
    many imports deep and how many blocks deep."""

    url: str | None = None
    maybe: bool = False
    layer: tuple[str, ...] = ()
    depth: int = 0
    nesting: int = 0


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
        self.animations: dict[str, Keyframes] = {}
        """The ``@keyframes`` of the scope by name: what they do to text."""
        self.registered: dict[str, Registration | None] = {}
        """The custom properties the scope's ``@property`` rules register. Outside the document
        (a shadow tree, the browser's own sheet), and under a condition the reader cannot
        evaluate, what a registered property holds is unknown."""
        self._document = document and not user_agent
        self._merged: tuple[Registered, Registered] | None = None
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
        registered = self._registering(parent.registered)
        unknown = registered
        if ua and (own := user_agent_sheets().get(element.namespace)) is not None:
            unknown = own._registering(registered)
            for rule in own._candidates(element):
                _offer(rule, matches(rule.selector, element), best, maybe)
        if element.namespace == SVG:
            for prop in _PRESENTATION.intersection(element.attrs):
                rank: Rank = (1, 1, 0, (-2,), (0, 0, 0), 0)
                value = _presentation(prop, " ".join(element.attrs[prop].lower().split()))
                _offer(Rule(_UNIVERSAL, prop, value, rank, False), YES, best, maybe)
        for rule in self._candidates(element):
            _offer(rule, matches(rule.selector, element), best, maybe)
        if host is not None and proxy is not None:
            for rule in host._candidates(proxy):
                inner = rule.rank
                context = 2 if inner[0] >= 2 else 0
                moved = replace(rule, rank=(inner[0], context, *inner[2:]))
                _offer(moved, matches(rule.selector, proxy), best, maybe)
        inline = declarations(element.attrs.get("style") or "")
        for order, (prop, value, important) in enumerate(inline):
            rank = (2 if important else 1, 1, 1, (_TOP,), (0, 0, 0), order)
            _offer(Rule(_UNIVERSAL, prop, value, rank, False), YES, best, maybe)
        svg = element.namespace == SVG
        if not best and not maybe:
            if parent.svg == svg and registered is parent.registered:
                return parent
            return replace(parent, svg=svg, registered=registered)
        custom = _substitute(best, maybe, parent.custom, unknown)
        present = frozenset(best).union(maybe)
        computed = compute(Cascade(best, maybe, present), parent, self._viewport)
        if computed.removed and self.animations:
            shown = self._animated_display(Cascade(best, maybe, present))
            if shown:
                maybe = {**maybe, "display": [*maybe.get("display", ()), *shown]}
                present = present.union(("display",))
                computed = compute(Cascade(best, maybe, present), parent, self._viewport)
        if computed.removed:
            return computed
        if self.animations and not computed.unsure and self._animated(best, maybe):
            computed = replace(computed, unsure=True)
        if (placed := best.get("position")) is not None and placed.value in _POSITIONED:
            box = (element, _z_index(best.get("z-index")), placed.value)
            computed = replace(computed, boxes=(*computed.boxes, box))
        if computed.svg == svg and computed.custom is custom and computed.registered is registered:
            return computed
        return replace(computed, svg=svg, custom=custom, registered=registered)

    def covers(self, element: Element, parent: Computed) -> bool:
        """The page's styles may make `element`, under `parent`, a fixed or absolute box with
        an opaque background (`_opaque`) that spans the whole viewport (or its containing
        block), ``inset: 0`` or ``top`` and ``left`` 0 with a ``width`` and ``height`` of 100%,
        and that is not removed, hidden, faded or put out of sight (`stowed`). They may when
        its certain values do, or when the values one rule that may apply to it gives it on top
        of them do (a ``:hover`` or ``:target`` rule, an ``@media`` query the reader cannot
        evaluate), or all of those rules together, or a keyframe of an animation it may run
        (`_keyframes`): an overlay a fade-in or a hover reveals. A box whose only animation
        certainly comes to rest filling forwards (`_rested`) is read as it rests: a preloader
        that fades out."""
        best: dict[str, Rule] = {}
        maybe: dict[str, list[Rule]] = {}
        for rule in self._candidates(element):
            _offer(rule, matches(rule.selector, element), best, maybe)
        inline = declarations(element.attrs.get("style") or "")
        for order, (prop, value, important) in enumerate(inline):
            rank: Rank = (2 if important else 1, 1, 1, (_TOP,), (0, 0, 0), order)
            _offer(Rule(_UNIVERSAL, prop, value, rank, False), YES, best, maybe)
        cascade = Cascade(best, maybe, frozenset(best).union(maybe))
        bases = self._rested(best, cascade) if self.animations else [best]
        if any(self._covering(base, parent) for base in bases):
            return True
        if not maybe and "animation-name" not in best and "animation" not in best:
            return False
        groups: dict[int, list[Rule]] = {}
        for rules in maybe.values():
            for rule in rules:
                groups.setdefault(id(rule.selector), []).append(rule)
        changes = list(groups.values())
        if len(groups) > 1:
            changes.append(sorted((r for rules in maybe.values() for r in rules), key=_rank))
        if self.animations:
            changes.extend(self._keyframes(cascade))
        return any(
            self._covering(_over(base, change), parent) for base in bases for change in changes
        )

    def _rested(self, best: dict[str, Rule], cascade: Cascade) -> list[dict[str, Rule]]:
        """`best` with the end keyframes the element's only animation rests on put over it, one
        state for each, when the animation certainly comes to rest filling forwards (`resting`,
        `fills_forwards`) and every rule that may apply names the same one; else `best`
        alone."""
        names = set(cascade.values("animation-name"))
        if cascade.sets("animation") or len(names) != 1:
            return [best]
        (value,) = names
        items = split_commas(value) if value is not None else []
        frames = self.animations.get(items[0].strip("\"'")) if len(items) == 1 else None
        if frames is None or not fills_forwards(cascade):
            return [best]
        rest = resting(cascade, frames)
        if not rest:
            return [best]
        return [
            _over(
                best,
                _animated(
                    d for offsets, declared in frames.frames if end in offsets for d in declared
                ),
            )
            for end in rest
        ]

    def _covering(self, rules: dict[str, Rule], parent: Computed) -> bool:
        """Whether a box styled by `rules` alone, under `parent`, covers what is under it."""
        values = {prop: rule.value for prop, rule in rules.items() if rule.value is not None}
        if values.get("position") not in ("absolute", "fixed") or not _opaque(values):
            return False
        viewport = self._viewport
        if stowed(values, viewport):
            return False
        spans = all(
            _flush(values.get(start), viewport)
            and (_flush(values.get(end), viewport) or _full(values.get(size), viewport, side))
            for start, end, size, side in (
                ("top", "bottom", "height", 1),
                ("left", "right", "width", 0),
            )
        )
        if not spans:
            return False
        seen = compute(Cascade(rules, {}, frozenset(rules)), parent, viewport)
        return not seen.removed and seen.visibility != HIDDEN and seen.alpha > FAINT_ALPHA

    def _keyframes(self, cascade: Cascade) -> Iterator[list[Rule]]:
        """The declarations of each keyframe an animation the element may run may give it, as
        rules that beat all but ``!important`` ones: for an element's only animation that comes
        to rest (`resting`), the end keyframes it rests on; else each keyframe in turn, and all
        of them together. An ``animation`` shorthand that holds a ``var()`` may run any."""
        held = (None,) if cascade.sets("animation") else ()
        for value in (*cascade.values("animation-name"), *held):
            if value is None:
                named, only = list(self.animations.values()), False
            else:
                names = [name.strip("\"'") for name in split_commas(value)]
                named = [frames for name in names if (frames := self.animations.get(name))]
                only = len(names) == 1
            for frames in named:
                rest = resting(cascade, frames) if only else None
                if rest is None:
                    yield from (_animated(declared) for _, declared in frames.frames)
                    yield _animated(d for _, declared in frames.frames for d in declared)
                    continue
                for end in rest:
                    yield _animated(
                        d for offsets, declared in frames.frames if end in offsets for d in declared
                    )

    def _animated_display(self, cascade: Cascade) -> list[Rule]:
        """The ``display`` values other than ``none`` a keyframe of an animation the element may
        run gives it: an element its own styles remove that an animation may show."""
        return [
            rule
            for rules in self._keyframes(cascade)
            for rule in rules
            if rule.prop == "display" and rule.value != "none"
        ]

    def _certain(self, element: Element) -> dict[str, str]:
        """The values the rules that certainly apply to `element` give it."""
        best: dict[str, Rule] = {}
        for rule in self._candidates(element):
            if matches(rule.selector, element) == YES and not rule.maybe:
                _offer(rule, YES, best, {})
        inline = declarations(element.attrs.get("style") or "")
        for order, (prop, value, important) in enumerate(inline):
            rank: Rank = (2 if important else 1, 1, 1, (_TOP,), (0, 0, 0), order)
            _offer(Rule(_UNIVERSAL, prop, value, rank, False), YES, best, {})
        return {prop: rule.value for prop, rule in best.items() if rule.value is not None}

    def collapsed(self, element: Element) -> bool:
        """The page's styles certainly give `element` a box at most a pixel wide or high, or
        place it far off-screen (a 0x0 analytics frame, one parked at ``left: -9999px``)."""
        values = self._certain(element)
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
        if context.nesting > MAX_NESTING:
            self.unknown = True
            return
        inner = replace(context, nesting=context.nesting + 1)
        for prelude, body in blocks(text):
            at_rule = _AT_RULE.match(prelude)
            if body is None:
                if parents:
                    self._declare(parents, prelude, context)
                elif at_rule is not None:
                    self._statement(at_rule.group(1).lower(), at_rule.group(2), context)
                continue
            if at_rule is not None:
                self._block(at_rule.group(1).lower(), at_rule.group(2), body, parents, inner)
                continue
            selectors = self._selectors(prelude, parents, body)
            if selectors:
                self._read(body, selectors, inner)

    def _statement(self, name: str, condition: str, context: _Context) -> None:
        if name == "layer":
            for layer in split_commas(condition):
                self._layer((*context.layer, *layer.split(".")))
        elif name == "import":
            self._import(condition, context)

    def _block(
        self, name: str, condition: str, body: str, parents: list[Selector], context: _Context
    ) -> None:
        if name == "property":
            self._register(condition.strip(), body, context)
            return
        if name in ("keyframes", "-webkit-keyframes"):
            name = unescape(condition).strip().strip("\"'").lower()
            before = self.animations.get(name, Keyframes())
            self.animations[name] = read_keyframes(body, self._viewport, before)
            return
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

    def _animated(self, best: dict[str, Rule], maybe: dict[str, list[Rule]]) -> bool:
        """Whether an animation the element may run may leave its text hidden: one whose
        keyframes may hide it and that may not come to rest where it shows (`settles_shown`),
        one of several, or one the reader cannot name."""
        cascade = Cascade(best, maybe, frozenset(best).union(maybe))
        for value in cascade.values("animation-name"):
            if value is None:
                return any(frames.hides for frames in self.animations.values())
            names = [name.strip("\"'") for name in split_commas(value)]
            hiding = [
                frames for name in names if (frames := self.animations.get(name)) and frames.hides
            ]
            if hiding and (len(names) > 1 or not settles_shown(cascade, hiding[0])):
                return True
        return False

    def _register(self, name: str, body: str, context: _Context) -> None:
        """Read an ``@property`` rule (one with a missing or invalid descriptor is ignored)."""
        name = unescape(name).strip() if "\\" in name else name
        if not name.startswith("--"):
            return
        descriptors: dict[str, str] = {}
        for declaration, nested in blocks(body):
            key, colon, value = declaration.partition(":")
            if nested is None and colon:
                descriptors[unescape(key).strip().lower()] = " ".join(unescape(value).split())
        syntax = descriptors.get("syntax", "").strip("\"'").strip()
        inherits = descriptors.get("inherits", "").lower()
        initial = descriptors.get("initial-value")
        if not syntax or inherits not in ("true", "false") or (initial is None and syntax != "*"):
            return
        if initial is not None and ("var(" in initial or "env(" in initial):
            return
        if not self._document or context.maybe:
            self.registered[name] = None
            return
        self.registered[name] = Registration(
            inherits == "true", None if initial is None else initial.lower()
        )

    def _registering(self, inherited: Registered) -> Registered:
        """`inherited` with this scope's registrations added."""
        if not self.registered:
            return inherited
        if self._merged is not None and any(inherited is m for m in self._merged):
            return self._merged[1]
        merged = {**inherited, **self.registered}
        self._merged = (inherited, merged)
        return merged

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


_POSITIONED: Final = words("absolute fixed relative sticky")


def _z_index(rule: Rule | None) -> int | None:
    """A ``z-index``: 0 for ``auto``, None when the reader cannot read it."""
    if rule is None or rule.value in ("auto", "initial", "unset", "revert", "revert-layer"):
        return 0
    found = parse_number(rule.value or "")
    if found is None or found[1] or found[0] != int(found[0]):
        return None
    return int(found[0])


def _opaque(values: dict[str, str]) -> bool:
    """Whether a box's certain `values` paint its background opaque all over: an opaque colour,
    or an image layer that covers it with an opaque gradient or a picture (a ``url()`` the
    reader cannot see counts as opaque), or a gradient it cannot read."""
    colour = parse_color(values.get("background-color") or "")
    if colour is not None and colour[3] >= 1:
        return True
    sizes = split_commas(values.get("background-size") or "")
    for at, layer in enumerate(split_commas(values.get("background-image") or "none")):
        size = sizes[at % len(sizes)] if sizes else None
        try:
            if background_images(layer, [size], None, None, (BLACK,)) is not None:
                return True
        except UnreadableError:
            return True
    return False


def _flush(value: str | None, viewport: Viewport | None) -> bool:
    """Whether an offset puts a box's edge at or past its containing block's."""
    if value is None:
        return False
    share = parse_percent(value)
    px = share if share is not None else length_px(value, viewport)
    return px is not None and px <= 0


def _full(value: str | None, viewport: Viewport | None, side: int) -> bool:
    """Whether a size spans the whole containing block (a share of 100% or more, or the
    viewport's size)."""
    if value is None:
        return False
    share = parse_percent(value)
    if share is not None:
        return share >= 100
    px = length_px(value, viewport)
    return px is not None and viewport is not None and px >= viewport[side]


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


def _presentation(prop: str, value: str) -> str | None:
    """An SVG presentation attribute's value as CSS: bare font sizes and ``transform`` numbers
    in user units (pixels) and degrees, its comma or space separated arguments as CSS's."""
    if prop == "font-size" and parse_number(value) is not None:
        return f"{value}px" if not (parse_number(value) or (0, ""))[1] else value
    if prop != "transform" or value == "none":
        return value
    found = functions(value)
    if not found:
        return None
    out: list[str] = []
    for name, args in found:
        unit = _SVG_UNITS.get(name, "")
        parts = [p for p in re.split(r"[\s,]+", args) if p]
        if name == "rotate":
            parts = parts[:1]
        out.append(f"{name}({', '.join(_with_unit(p, unit) for p in parts)})")
    return " ".join(out)


def _with_unit(number: str, unit: str) -> str:
    found = parse_number(number)
    return f"{number}{unit}" if unit and found is not None and not found[1] else number


def _offer(rule: Rule, matched: int, best: dict[str, Rule], maybe: dict[str, list[Rule]]) -> None:
    if matched == NO:
        return
    if matched == YES and not rule.maybe:
        current = best.get(rule.prop)
        if current is None or rule.rank > current.rank:
            best[rule.prop] = rule
    else:
        maybe.setdefault(rule.prop, []).append(rule)


_ANIMATION: Final[Rank] = (1, 2, 0, (_TOP,), (0, 0, 0), 0)
"""The rank of a keyframe's declaration: above every normal declaration, below important
ones."""


def _animated(declared: Iterable[tuple[str, str | None]]) -> list[Rule]:
    return [Rule(_UNIVERSAL, prop, value, _ANIMATION, False) for prop, value in declared]


def _rank(rule: Rule) -> Rank:
    return rule.rank


def _over(best: dict[str, Rule], rules: Iterable[Rule]) -> dict[str, Rule]:
    """`best` with each of `rules` that would beat its winner put in its place."""
    state = dict(best)
    for rule in rules:
        current = state.get(rule.prop)
        if rule.value is not None and (current is None or rule.rank >= current.rank):
            state[rule.prop] = rule
    return state


def _substitute(
    best: dict[str, Rule],
    maybe: dict[str, list[Rule]],
    inherited: Custom,
    registered: Registered,
) -> Custom:
    """Work out the element's custom properties, take them out of `best` and `maybe`, and
    substitute them (and ``env()``) into the rest, expanding the shorthands that waited on
    them."""
    names = [p for p in (*best, *maybe) if p.startswith("--")]
    custom: Custom = inherited
    local: Custom = {}
    if names:
        cascade = Cascade(best, maybe, frozenset(names))
        custom, local = custom_properties(
            {n: cascade.values(n) for n in names}, inherited, registered
        )
        for name in names:
            best.pop(name, None)
            maybe.pop(name, None)
    for prop, rules in maybe.items():
        if any(references(r.value) for r in rules):
            maybe[prop] = [v for r in rules for v in resolve_rule(r, custom, local, registered)]
    for prop, rule in list(best.items()):
        if references(rule.value):
            first, *rest = resolve_rule(rule, custom, local, registered)
            best[prop] = first
            if rest:
                maybe.setdefault(prop, []).extend(rest)
    if SHORTHANDS.isdisjoint(best) and SHORTHANDS.isdisjoint(maybe):
        return custom
    for prop in SHORTHANDS.intersection(best).union(SHORTHANDS.intersection(maybe)):
        waiting = [(r, YES) for r in ([best.pop(prop)] if prop in best else [])]
        waiting += [(r, MAYBE) for r in maybe.pop(prop, [])]
        for rule, matched in waiting:
            if rule.value is None:
                longhands = [(name, None) for name, _ in expand(prop, "unset")]
            else:
                longhands = list(expand(prop, rule.value))
            for name, value in longhands:
                _offer(replace(rule, prop=name, value=value), matched, best, maybe)
    return custom


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
