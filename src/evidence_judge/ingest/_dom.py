"""Decode Playwright frame snapshots (NodeSnapshot trees) into page text.

A snapshot's ``html`` is a NodeSnapshot: a text string, an element
``[name, attributes?, *children]``, or a subtree back-reference ``[[offset, index]]`` that
points at node ``index`` (post-order) of snapshot ``current - offset`` of the same frame.
This mirrors ``snapshotRenderer.ts`` in Playwright's isomorphic trace package, except that
offset-0 (same-snapshot) references are rejected: Playwright never writes them and they are
the only way to build a cycle.

Text typed into ``<input>`` and ``<textarea>`` controls (and their default values) is never page
text: the agent controls it, so a search box holding "Order #1042 confirmed" must not read as the
page confirming the order. Neither is the text of an editable element (``contenteditable``
with any value but "false"), descendants included: the agent can type into it like a field, nor
the selected ``<option>`` text of a ``<select>``: the agent picks it, so choosing "In stock" in a
filter must not read as the page saying the product is in stock. Button labels, which the page
authors, are kept.

Each piece of text is shown, hidden, or of unsure visibility, and only certainty hides it
(`_css`). Hidden: ``<script>``, ``<style>``, ``<template>`` and the like, the body of a closed
``<details>``, light-DOM children of a shadow host that no ``<slot>`` shows, what the browser's
own stylesheets hide unless the page's styles show it (``ua_hiding.css``, vendored from the HTML,
SVG and MathML specifications: the ``hidden`` attribute, a closed ``<dialog>``, ``<rp>``,
``<noembed>``, SVG ``<defs>``, ``<symbol>``, ``<mask>`` and the other never-rendered elements,
a ``<semantics>`` annotation, ``<mphantom>``, ...), text in an SVG element other than
``<text>``, and what the page's cascade certainly hides: ``display: none``, ``visibility:
hidden`` or ``content-visibility: hidden`` from inline styles, SVG presentation attributes,
``<style>`` elements, constructed stylesheets and the ``<link rel=stylesheet>`` and ``@import``
sheets the recording holds. Each shadow root is its own scope: its sheets style only its tree,
and the page's sheets do not reach into it. Unsure: text whose visibility depends on what the
reader does not model (see `_css`), including a ``popover`` (snapshots do not record whether it
is open), the content of a never-rendered SVG element something refers to (a ``<use>`` copy, a
pattern fill, a mask), a ``<switch>`` child other than an unconditional first one, and SVG text
placed past its canvas; and all text of a scope whose stylesheets the recording does not fully
hold. An empty custom element may hold a closed shadow root the snapshot cannot see, and a
shown frame of the site's own holds a document of its own, so a page with either may show text
the reader never saw (`RenderedPage.opaque`); a frame at most a pixel in size, parked
off-screen, or holding another site's page does not count.
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

from evidence_judge.core.site import registrable_domain
from evidence_judge.ingest._css import (
    HIDDEN,
    HTML,
    MATHML,
    REMOVED,
    ROOT,
    SHOWN,
    SVG,
    UNSURE,
    Computed,
    Element,
    Sheet,
    StyleSheets,
    StylesheetSource,
    Viewport,
    media_holds,
)
from evidence_judge.ingest._json import JsonObject, as_int, as_list, as_object, as_str

DEFAULT_MAX_VISITS = 1_000_000

_FRAMES = frozenset({"IFRAME", "FRAME"})
"""Elements that show another document, which the snapshot of this one does not hold."""
_SKIPPED_TAGS = frozenset(
    {
        "SCRIPT",
        "STYLE",
        "NOSCRIPT",
        "IFRAME",
        "FRAME",
        "OBJECT",
        "EMBED",
        "CANVAS",
        "VIDEO",
        "AUDIO",
        "DATALIST",
        "META",
        "LINK",
        "BASE",
        "TITLE",
        "DESC",
        "METADATA",
    }
)
"""Elements whose text never renders: TITLE here is one outside ``<head>`` (an SVG tooltip)."""
_INLINE_TAGS = frozenset(
    {
        "A",
        "ABBR",
        "B",
        "BDI",
        "BDO",
        "CITE",
        "CODE",
        "DATA",
        "DFN",
        "EM",
        "FONT",
        "I",
        "KBD",
        "LABEL",
        "MARK",
        "Q",
        "S",
        "SAMP",
        "SMALL",
        "SPAN",
        "STRONG",
        "SUB",
        "SUP",
        "TIME",
        "U",
        "VAR",
        "WBR",
        "SLOT",
    }
)
_SVG_TEXT: Final = frozenset({"TEXT", "FOREIGNOBJECT"})
"""SVG elements whose text renders: a text node elsewhere in an ``<svg>`` never does."""
_SVG_CONDITIONS: Final = frozenset({"requiredextensions", "requiredfeatures", "systemlanguage"})
"""Attributes that decide whether a ``<switch>`` renders a child."""
_MATH_TEXT: Final = frozenset({"MI", "MN", "MO", "MS", "MTEXT", "ANNOTATION-XML"})
"""MathML elements whose children may be HTML."""
_FRAGMENT_REF = re.compile(r"""url\(\s*['"]?#([^'")\s]+)""")
_NUMBER = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+))(?:px)?(?:[\s,]|$)")
_NON_TEXT_INPUTS = frozenset({"hidden", "password", "file", "checkbox", "radio", "image"})
_BUTTON_INPUTS = frozenset({"submit", "button", "reset"})
_SHADOW_ROOT_ATTR = "__playwright_shadow_root_"
_VALUE_ATTR = "__playwright_value_"
_SELECTED_ATTR = "__playwright_selected_"
_DIALOG_OPEN_ATTR = "__playwright_dialog_open_"
_STYLE_SHEET_ATTR = "__playwright_style_sheet_"
"""The text of a constructed (adopted) stylesheet, or of one a script changed."""
MAX_SNAPSHOT_CSS: Final = 8 * 1024 * 1024
"""How many characters of stylesheets one scope of a snapshot applies; beyond that its styles
are unknown."""

_CACHED_SHEETS: Final = 16
"""How many parsed stylesheet sets a store keeps (a run's pages share a few)."""


class SnapshotDecodeError(ValueError):
    """A snapshot's node tree expands beyond the visit budget (malformed or hostile input)."""


@dataclass(frozen=True, slots=True)
class RenderedPage:
    title: str | None
    text: str
    """The text that shows."""
    form_values: int = 0
    """Non-empty text-field, textarea, editable-element and selected-option values left out of
    ``text``."""
    field_values: tuple[str, ...] = ()
    """Those values, whitespace-normalised, in document order."""
    unsure_text: str = ""
    """The text whose visibility the reader is unsure of, in document order."""
    opaque: bool = False
    """The page may show text the snapshot does not hold (a closed shadow root, text a
    ``::before`` or ``::after`` rule gives a shown element, a shown frame's document)."""


_BLOCK_END = object()


@dataclass(frozen=True, slots=True)
class _Child:
    """A child node, resolved, with the element the cascade sees for it (None for text)."""

    node: str | list[Any]
    index: int
    element: Element | None


@dataclass(frozen=True, slots=True)
class _Scope:
    """A tree scope: the document, or one shadow root."""

    sheets: StyleSheets
    unknown: bool
    """Its styles are partly unknown: every element in it is unsure."""
    outer: _Scope | None = None
    """For a shadow root: the scope of its host, where the host's light children are styled."""
    slots: dict[str, list[_Child]] | None = None
    """For a shadow root: the host's light children by the slot name they ask for."""
    first_slots: frozenset[int] = frozenset()
    """The ids of the slot elements that get assigned children (the first of each name)."""
    parts: bool = False
    """An enclosing scope has ``::part()`` rules that may hide elements of this one."""


_Item = tuple[object, int, Element | None, Computed, bool, _Scope]
"""A node to render: the node, its snapshot, the element the cascade sees for it, what its
parent decided, whether it is inside a closed ``<details>`` (only a ``<summary>`` shows), and its
tree scope."""


class FrameSnapshotStore:
    """Snapshot history of every frame, in recording order, for resolving back-references."""

    def __init__(self, *, max_visits: int = DEFAULT_MAX_VISITS) -> None:
        self.max_visits = max_visits
        self._roots: dict[str, list[object]] = {}
        self._nodes: dict[tuple[str, int], list[object]] = {}
        self._sheets: dict[tuple[tuple[Sheet, ...], Viewport | None, bool], StyleSheets] = {}

    def add(self, frame_id: str, html: object) -> int:
        roots = self._roots.setdefault(frame_id, [])
        roots.append(html)
        return len(roots) - 1

    def root(self, frame_id: str, index: int) -> object:
        return self._roots[frame_id][index]

    def render(
        self,
        frame_id: str,
        index: int,
        *,
        viewport: Viewport | None = None,
        stylesheets: StylesheetSource | None = None,
        url: str | None = None,
    ) -> RenderedPage:
        """Page text of snapshot `index` of `frame_id`, at `url`; `viewport` evaluates
        ``@media`` rules and `stylesheets` supplies linked and imported stylesheets."""
        return _Renderer(self, frame_id, viewport, stylesheets, url).render(index)

    def forget_styles(self) -> None:
        """Drop parsed stylesheets: the next renders read another context's stylesheets."""
        self._sheets.clear()

    def style_sheets(
        self,
        sheets: tuple[Sheet, ...],
        viewport: Viewport | None,
        source: StylesheetSource | None,
        *,
        document: bool,
    ) -> StyleSheets:
        key = (sheets, viewport, document)
        cached = self._sheets.get(key)
        if cached is None:
            if len(self._sheets) >= _CACHED_SHEETS:
                del self._sheets[next(iter(self._sheets))]
            cached = StyleSheets(sheets, viewport, source=source, document=document)
            self._sheets[key] = cached
        return cached

    def resolve(self, frame_id: str, index: int, ref: object) -> tuple[object, int] | None:
        pair = as_list(ref)
        if len(pair) != 2:
            return None
        offset, node_index = as_int(pair[0]), as_int(pair[1])
        if offset is None or node_index is None:
            return None
        target = index - offset
        if offset < 1 or target < 0:
            return None
        nodes = self._post_order(frame_id, target)
        if not 0 <= node_index < len(nodes):
            return None
        return nodes[node_index], target

    def _post_order(self, frame_id: str, index: int) -> list[object]:
        key = (frame_id, index)
        cached = self._nodes.get(key)
        if cached is not None:
            return cached
        nodes: list[object] = []
        stack: list[tuple[object, bool]] = [(self.root(frame_id, index), False)]
        while stack:
            node, expanded = stack.pop()
            if isinstance(node, str):
                nodes.append(node)
            elif _element(node) is not None:
                if expanded:
                    nodes.append(node)
                else:
                    stack.append((node, True))
                    stack.extend((child, False) for child in reversed(_children(node)))
        self._nodes[key] = nodes
        return nodes


class _Renderer:
    def __init__(
        self,
        store: FrameSnapshotStore,
        frame_id: str,
        viewport: Viewport | None,
        stylesheets: StylesheetSource | None,
        url: str | None,
    ) -> None:
        self._store = store
        self._frame_id = frame_id
        self._viewport = viewport
        self._stylesheets = stylesheets
        self._url = url
        self._document: str | None = url
        """The URL the document's relative URLs resolve against (its ``<base href>``)."""
        self._visits = 0
        self._field_values: list[str] = []
        self._shown: list[str] = []
        self._unsure: list[str] = []
        self._opaque = False
        self._fixed = ""
        self._referenced: set[str] = set()
        """Ids a ``<use>`` or a ``url(#id)`` refers to: what an SVG paint server, mask, marker or
        symbol copies onto the page."""

    def render(self, index: int) -> RenderedPage:
        root = self._store.root(self._frame_id, index)
        if (base := self._find_base(root, index)) is not None:
            with suppress(ValueError):
                self._document = urljoin(self._url or "", base)
        scope = self._scope([(root, index)], document=True)
        title: str | None = None
        start = ROOT if not scope.unknown else replace(ROOT, unsure=True)
        resolved = self._deref(root, index)
        stack: list[_Item] = []
        if resolved is not None:
            node, at = resolved
            element = None if isinstance(node, str) else _new_element(node, None)
            stack.append((node, at, element, start, False, scope))
        while stack:
            node, snapshot_index, element, inherited, folded, scope = stack.pop()
            self._tick()
            if node is _BLOCK_END:
                self._emit(" ", None)
                continue
            if isinstance(node, str):
                if not folded:
                    self._emit(node, inherited.text)
                continue
            node = _element(node)
            if element is None or node is None:
                continue
            tag, attrs = _tag_and_attrs(node)
            if tag == "HEAD":
                title = title or self._title(node, snapshot_index)
                continue
            if folded and tag != "SUMMARY":
                if not scope.sheets.details:
                    continue
                inherited, folded = replace(inherited, unsure=True), False
            self._element(
                node, snapshot_index, element, inherited, folded, scope, tag, attrs, stack
            )
        return RenderedPage(
            title=_normalise(title) or None,
            text=_normalise("".join(self._shown)),
            form_values=len(self._field_values),
            field_values=tuple(self._field_values),
            unsure_text=_normalise("".join(self._unsure)),
            opaque=self._opaque,
        )

    def _element(
        self,
        node: list[Any],
        snapshot_index: int,
        element: Element,
        inherited: Computed,
        folded: bool,
        scope: _Scope,
        tag: str,
        attrs: JsonObject,
        stack: list[_Item],
    ) -> None:
        if tag in _SKIPPED_TAGS or (tag == "TEMPLATE" and _SHADOW_ROOT_ATTR not in attrs):
            if tag in _FRAMES and not self._opaque:
                self._opaque = self._frame_shows(element, inherited, scope, attrs)
            return
        children = self._resolved(node, snapshot_index, parent=element)
        shadow = next(
            (
                c
                for c in children
                if c.element is not None
                and c.element.tag == "TEMPLATE"
                and _SHADOW_ROOT_ATTR in c.element.attrs
            ),
            None,
        )
        inner: _Scope | None = None
        proxy: Element | None = None
        if shadow is not None and isinstance(shadow.node, list):
            proxy = Element(tag, element.attrs, None, host=element)
            inner = self._shadow_scope(shadow, children, scope)
        host = inner.sheets if inner is not None else None
        style = scope.sheets.style(element, inherited, host=host, proxy=proxy)
        if style.removed and element.namespace == SVG and self._referenced:
            style = self._copied(node, snapshot_index, element, inherited, scope, host, proxy)
        if style.removed:
            return
        if element.namespace == SVG and tag == "TEXT" and _off_canvas(element, style):
            style = replace(style, unsure=True)
        if scope.parts and "part" in element.attrs and not style.unsure:
            style = replace(style, unsure=True)
        if ("-" in tag and not children) or (
            style.text != HIDDEN and scope.sheets.generates(element)
        ):
            self._opaque = True
        if tag not in _INLINE_TAGS:
            self._emit(" ", None)
            stack.append((_BLOCK_END, snapshot_index, None, style, False, scope))
        control_text = self._control_text(node, tag, attrs, snapshot_index)
        if control_text is not None:
            self._emit(control_text, style.text)
            if self._fixed:
                self._emit(f" {self._fixed} ", max(style.text, UNSURE))
                self._fixed = ""
            return
        closed = tag == "DETAILS" and "open" not in attrs
        if inner is not None and shadow is not None and isinstance(shadow.node, list):
            start = style if not inner.unknown else replace(style, unsure=True)
            rooted = self._resolved(shadow.node, shadow.index, parent=proxy)
            stack.extend(
                (c.node, c.index, c.element, start, closed, inner) for c in reversed(rooted)
            )
            return
        assigned = self._assigned(node, attrs, scope) if tag == "SLOT" else None
        if assigned and scope.outer is not None:
            passed = style if not scope.sheets.slotted else replace(style, unsure=True)
            stack.extend(
                (c.node, c.index, c.element, passed, False, scope.outer) for c in reversed(assigned)
            )
            return
        stack.extend(
            (c.node, c.index, c.element, s, closed, scope)
            for c, s in reversed(
                list(zip(children, _child_styles(element, style, children), strict=True))
            )
        )

    def _frame_shows(
        self, element: Element, inherited: Computed, scope: _Scope, attrs: JsonObject
    ) -> bool:
        """A frame may show text of the site's own that this snapshot does not hold: it loads
        a document of the same site into a box bigger than a pixel that is not off-screen. A
        third-party frame (a payment field, a video, an analytics beacon) holds the third
        party's page, not the site's."""
        if not _loads_content(attrs) or _tiny(attrs) or not self._same_site(attrs):
            return False
        style = scope.sheets.style(element, inherited)
        return not style.removed and style.text != HIDDEN and not scope.sheets.collapsed(element)

    def _same_site(self, attrs: JsonObject) -> bool:
        if as_str(attrs.get("srcdoc")) or self._url is None:
            return True
        try:
            frame = urlsplit(urljoin(self._document or self._url, as_str(attrs.get("src")) or ""))
            page = urlsplit(self._url)
        except ValueError:
            return True
        if not frame.hostname or not page.hostname or frame.scheme not in ("http", "https"):
            return True
        return registrable_domain(frame.hostname) == registrable_domain(page.hostname)

    def _copied(
        self,
        node: list[Any],
        snapshot_index: int,
        element: Element,
        inherited: Computed,
        scope: _Scope,
        host: StyleSheets | None,
        proxy: Element | None,
    ) -> Computed:
        """The style of an SVG element the browser's own rules remove (``<symbol>``,
        ``<defs>``, ``<pattern>``, ``<mask>``, ...): unsure when something on the page refers
        to it or into it, as a ``<use>`` copy, a fill or a mask shows its content."""
        own = scope.sheets.style(element, inherited, ua=False, host=host, proxy=proxy)
        if own.removed or not self._refers_into(node, snapshot_index):
            return REMOVED
        return replace(own, unsure=True)

    def _refers_into(self, node: list[Any], snapshot_index: int) -> bool:
        stack: list[tuple[object, int]] = [(node, snapshot_index)]
        while stack:
            child, index = stack.pop()
            self._tick()
            resolved = self._deref(child, index)
            target = None if resolved is None else _element(resolved[0])
            if resolved is None or target is None:
                continue
            if as_str(_tag_and_attrs(target)[1].get("id")) in self._referenced:
                return True
            stack.extend((c, resolved[1]) for c in _children(target))
        return False

    @staticmethod
    def _assigned(node: list[Any], attrs: JsonObject, scope: _Scope) -> list[_Child] | None:
        """The light children a slot shows, if it is the first of its name in a shadow tree."""
        if scope.slots is None or id(node) not in scope.first_slots:
            return None
        return scope.slots.get(as_str(attrs.get("name")) or "")

    def _emit(self, text: str, state: int | None) -> None:
        """Add `text` to the shown or unsure text; a block break (state None) to both."""
        if state is None:
            self._shown.append(text)
            self._unsure.append(text)
        elif state == SHOWN:
            self._shown.append(text)
            if self._unsure and self._unsure[-1] != " ":
                self._unsure.append(" ")
        elif state != HIDDEN:
            self._unsure.append(text)
            if self._shown and self._shown[-1] != " ":
                self._shown.append(" ")

    def _resolved(
        self, node: list[Any], snapshot_index: int, *, parent: Element | None = None
    ) -> list[_Child]:
        """The children of `node`, resolved; elements get the Element the cascade sees, with
        `parent` and their element siblings (a shadow root and constructed sheets are not
        among them)."""
        out: list[_Child] = []
        siblings: list[Element] = []
        for child in _children(node):
            resolved = self._deref(child, snapshot_index)
            if resolved is None:
                continue
            target, index = resolved
            if isinstance(target, str):
                out.append(_Child(target, index, None))
                continue
            tag, attrs = _tag_and_attrs(target)
            element = Element(tag, _text_attrs(attrs), parent, namespace=_namespace(tag, parent))
            if tag != "TEMPLATE" or (
                _SHADOW_ROOT_ATTR not in attrs and _STYLE_SHEET_ATTR not in attrs
            ):
                element.siblings, element.index = siblings, len(siblings)
                siblings.append(element)
            out.append(_Child(target, index, element))
        return out

    def _shadow_scope(self, shadow: _Child, children: list[_Child], outer: _Scope) -> _Scope:
        """The scope of a shadow root, with the host's light children (`children`) by slot."""
        root = shadow.node
        assert isinstance(root, list)
        sheets, unknown = self._scope_sheets([(c, shadow.index) for c in _children(root)])
        styles = self._store.style_sheets(sheets, self._viewport, self._stylesheets, document=False)
        slots: dict[str, list[_Child]] = {}
        for child in children:
            if child is shadow:
                continue
            name = "" if child.element is None else child.element.attrs.get("slot", "")
            slots.setdefault(name, []).append(child)
        return _Scope(
            styles,
            unknown or styles.unknown,
            outer,
            slots,
            self._first_slots(root, shadow.index),
            outer.parts or outer.sheets.parts,
        )

    def _first_slots(self, root: list[Any], snapshot_index: int) -> frozenset[int]:
        """The ids of the first ``<slot>`` of each name in a shadow tree, in tree order."""
        seen: dict[str, int] = {}
        stack: list[tuple[object, int]] = [(c, snapshot_index) for c in reversed(_children(root))]
        while stack:
            node, index = stack.pop()
            self._tick()
            resolved = self._deref(node, index)
            target = None if resolved is None else _element(resolved[0])
            if resolved is None or target is None:
                continue
            at = resolved[1]
            if _is_shadow_root(target):
                continue
            tag, attrs = _tag_and_attrs(target)
            if tag == "SLOT":
                seen.setdefault(as_str(attrs.get("name")) or "", id(target))
            stack.extend((c, at) for c in reversed(_children(target)))
        return frozenset(seen.values())

    def _scope(self, roots: list[tuple[object, int]], *, document: bool) -> _Scope:
        sheets, unknown = self._scope_sheets(roots)
        styles = self._store.style_sheets(
            sheets, self._viewport, self._stylesheets, document=document
        )
        return _Scope(styles, unknown or styles.unknown)

    def _scope_sheets(self, roots: list[tuple[object, int]]) -> tuple[tuple[Sheet, ...], bool]:
        """The stylesheets a tree scope applies, in document order (``<style>`` text, constructed
        sheets, and the linked sheets the recording holds, each unless its ``media`` cannot
        hold), and whether part of its styles is unknown: a linked sheet the recording does not
        hold, or more than MAX_SNAPSHOT_CSS characters of them."""
        found: list[Sheet] = []
        size = 0
        unknown = False
        stack = list(reversed(roots))
        while stack:
            node, snapshot_index = stack.pop()
            self._tick()
            resolved = self._deref(node, snapshot_index)
            if resolved is None:
                continue
            element, snapshot_index = resolved
            if isinstance(element, str):
                continue
            tag, attrs = _tag_and_attrs(element)
            if _is_shadow_root(element):
                continue
            self._references(tag, attrs)
            sheet = self._sheet(element, tag, attrs, snapshot_index)
            if sheet is not None:
                text, url = sheet
                if text:
                    self._referenced.update(_FRAGMENT_REF.findall(text))
                holds = media_holds(as_str(attrs.get("media")) or "", self._viewport)
                if text is None and holds is not False:
                    unknown = True
                elif text and holds is not False:
                    if size + len(text) > MAX_SNAPSHOT_CSS:
                        unknown = True
                        text = text[: MAX_SNAPSHOT_CSS - size]
                    found.append(Sheet(text, url, holds is None))
                    size += len(text)
            if tag not in ("STYLE", "SCRIPT", "NOSCRIPT", "TEMPLATE"):
                stack.extend((child, snapshot_index) for child in reversed(_children(element)))
        return tuple(found), unknown

    def _references(self, tag: str, attrs: JsonObject) -> None:
        """Note the ids an element refers to: a ``<use>`` its ``href``, any attribute its
        ``url(#id)`` values."""
        for name, value in attrs.items():
            if not isinstance(value, str) or name.startswith("__playwright"):
                continue
            if tag == "USE" and name.lower() in ("href", "xlink:href") and value.startswith("#"):
                self._referenced.add(value[1:])
            elif "url(" in value:
                self._referenced.update(_FRAGMENT_REF.findall(value))

    def _sheet(
        self, node: list[Any], tag: str, attrs: JsonObject, snapshot_index: int
    ) -> tuple[str | None, str | None] | None:
        """(text, URL) of the stylesheet `node` is, text None when the recording does not hold
        it; None when `node` is not a stylesheet."""
        document = self._document
        if (constructed := as_str(attrs.get(_STYLE_SHEET_ATTR))) is not None:
            return constructed, document
        if tag == "STYLE":
            return self._text_content(node, snapshot_index), document
        rel = (as_str(attrs.get("rel")) or "").lower().split()
        href = as_str(attrs.get("href"))
        if tag != "LINK" or "stylesheet" not in rel or "alternate" in rel or href is None:
            return None
        if "disabled" in attrs:
            return None
        try:
            url = urljoin(document or "", href)
        except ValueError:
            return None
        return (None if self._stylesheets is None else self._stylesheets(url)), url

    def _find_base(self, root: object, index: int) -> str | None:
        """The first ``<base href>`` of the document."""
        stack: list[tuple[object, int]] = [(root, index)]
        while stack:
            node, snapshot_index = stack.pop()
            self._tick()
            resolved = self._deref(node, snapshot_index)
            element = None if resolved is None else _element(resolved[0])
            if resolved is None or element is None:
                continue
            at = resolved[1]
            tag, attrs = _tag_and_attrs(element)
            if tag == "BASE" and (href := as_str(attrs.get("href"))) is not None:
                return href
            if tag in ("HTML", "HEAD"):
                stack.extend((c, at) for c in reversed(_children(element)))
        return None

    def _tick(self) -> None:
        self._visits += 1
        if self._visits > self._store.max_visits:
            raise SnapshotDecodeError(
                f"snapshot expands to more than {self._store.max_visits} nodes"
            )

    def _deref(self, node: object, snapshot_index: int) -> tuple[str | list[Any], int] | None:
        """Follow subtree references until a text or element node (or nothing) is reached."""
        while True:
            if isinstance(node, str):
                return node, snapshot_index
            element = _element(node)
            if element is not None:
                return element, snapshot_index
            items = as_list(node)
            if not items:
                return None
            resolved = self._store.resolve(self._frame_id, snapshot_index, items[0])
            if resolved is None:
                return None
            node, snapshot_index = resolved
            self._tick()

    def _text_content(self, node: list[Any], snapshot_index: int) -> str:
        parts: list[str] = []
        stack: list[tuple[object, int]] = [
            (child, snapshot_index) for child in reversed(_children(node))
        ]
        while stack:
            child, index = stack.pop()
            self._tick()
            resolved = self._deref(child, index)
            if resolved is None:
                continue
            target, target_index = resolved
            if isinstance(target, str):
                parts.append(target)
            else:
                stack.extend((c, target_index) for c in reversed(_children(target)))
        return "".join(parts)

    def _resolved_children(
        self, node: list[Any], snapshot_index: int
    ) -> list[tuple[list[Any], int]]:
        resolved: list[tuple[list[Any], int]] = []
        for child in _children(node):
            target = self._deref(child, snapshot_index)
            if target is not None and not isinstance(target[0], str):
                resolved.append((target[0], target[1]))
        return resolved

    def _title(self, head: list[Any], snapshot_index: int) -> str | None:
        for child, index in self._resolved_children(head, snapshot_index):
            if _tag_and_attrs(child)[0] == "TITLE":
                return self._text_content(child, index)
        return None

    def _control_text(
        self, node: list[Any], tag: str, attrs: JsonObject, snapshot_index: int
    ) -> str | None:
        if tag == "INPUT":
            kind = (as_str(attrs.get("type")) or "text").lower()
            if kind in _NON_TEXT_INPUTS:
                return ""
            if kind in _BUTTON_INPUTS:
                return as_str(attrs.get("value")) or ""
            value = as_str(attrs.get(_VALUE_ATTR)) or as_str(attrs.get("value"))
            return self._excluded_value(value, fixed=_is_fixed(attrs))
        if tag == "TEXTAREA":
            value = as_str(attrs.get(_VALUE_ATTR))
            if value is None:
                value = self._text_content(node, snapshot_index)
            return self._excluded_value(value, fixed=_is_fixed(attrs))
        if tag == "SELECT":
            return self._excluded_value(self._selected_option_text(node, snapshot_index))
        if _is_editable(attrs):
            return self._excluded_value(self._text_content(node, snapshot_index))
        return None

    def _excluded_value(self, value: str | None, *, fixed: bool = False) -> str:
        """Keep a field's value out of the text, as a field value. A `fixed` field's value (one
        the agent cannot type into) is text the page may show: it is the site's, but it is a
        field value too, so it never proves a predicate; it only keeps a phrase's absence from
        contradicting one."""
        if value := _normalise(value):
            self._field_values.append(value)
            if fixed:
                self._fixed = value
        return ""

    def _selected_option_text(self, select: list[Any], snapshot_index: int) -> str:
        options: list[tuple[list[Any], int]] = []
        for child, index in self._resolved_children(select, snapshot_index):
            tag = _tag_and_attrs(child)[0]
            if tag == "OPTION":
                options.append((child, index))
            elif tag == "OPTGROUP":
                options.extend(
                    (opt, i)
                    for opt, i in self._resolved_children(child, index)
                    if _tag_and_attrs(opt)[0] == "OPTION"
                )
        if not options:
            return ""
        chosen = [o for o in options if _is_selected(_tag_and_attrs(o[0])[1])] or options[:1]
        return " ".join(self._text_content(option, index) for option, index in chosen)


def _element(node: object) -> list[Any] | None:
    items = as_list(node)
    return items if items and isinstance(items[0], str) else None


def _children(node: object) -> list[Any]:
    return as_list(node)[2:]


def _tag_and_attrs(node: list[Any]) -> tuple[str, JsonObject]:
    attrs = as_object(node[1]) if len(node) > 1 else {}
    return str(node[0]).upper(), attrs


def _new_element(node: list[Any], parent: Element | None) -> Element:
    tag, attrs = _tag_and_attrs(node)
    return Element(tag, _text_attrs(attrs), parent)


def _is_shadow_root(node: object) -> bool:
    element = _element(node)
    if element is None:
        return False
    tag, attrs = _tag_and_attrs(element)
    return tag == "TEMPLATE" and _SHADOW_ROOT_ATTR in attrs


def _namespace(tag: str, parent: Element | None) -> str:
    """The namespace an element with `tag` under `parent` is in, as an HTML parser decides."""
    above = HTML if parent is None else parent.namespace
    if above == SVG and parent is not None and parent.tag != "FOREIGNOBJECT":
        return SVG
    if above == MATHML and parent is not None and parent.tag not in _MATH_TEXT:
        return MATHML
    return {"SVG": SVG, "MATH": MATHML}.get(tag, HTML)


def _child_styles(element: Element, style: Computed, children: list[_Child]) -> list[Computed]:
    """What each child of `element` inherits: text in an SVG element other than ``<text>``
    never renders, and a ``<switch>`` renders one child: its first, when that has no
    conditions (``systemLanguage``, ...), for certain; any other maybe."""
    if element.namespace != SVG:
        return [style] * len(children)
    text_style = style if _in_svg_text(element) else replace(style, visibility=HIDDEN)
    styles = [text_style if c.element is None else style for c in children]
    if element.tag == "SWITCH":
        first = True
        for at, child in enumerate(children):
            if child.element is None:
                continue
            attrs = child.element.attrs
            if not first or not _SVG_CONDITIONS.isdisjoint(k.lower() for k in attrs):
                styles[at] = replace(style, unsure=True)
            first = False
    return styles


def _in_svg_text(element: Element) -> bool:
    found: Element | None = element
    while found is not None and found.namespace == SVG:
        if found.tag in _SVG_TEXT:
            return True
        found = found.parent
    return False


def _off_canvas(text: Element, style: Computed) -> bool:
    """An SVG ``<text>`` placed past the right or bottom edge of its ``<svg>``'s box (its
    ``viewBox``, else its ``width`` and ``height``)."""
    svg = text.parent
    while svg is not None and svg.tag != "SVG":
        svg = svg.parent
    if svg is None:
        return False
    view_box = svg.attrs.get("viewBox") or svg.attrs.get("viewbox") or ""
    box = [n for v in view_box.replace(",", " ").split() if (n := _svg_number(v)) is not None]
    if len(box) == 4:
        right, bottom = box[0] + box[2], box[1] + box[3]
    else:
        width, height = _svg_number(svg.attrs.get("width")), _svg_number(svg.attrs.get("height"))
        if width is None or height is None:
            return False
        right, bottom = width, height
    x, y = _svg_number(text.attrs.get("x")), _svg_number(text.attrs.get("y"))
    return (x is not None and x >= right) or (
        y is not None and y >= bottom + (style.font_px or 16.0)
    )


def _svg_number(value: str | None) -> float | None:
    """The first number of an SVG length or list (``"12"``, ``"12px"``, ``"12 30"``)."""
    found = _NUMBER.match(value or "")
    return None if found is None else float(found.group(1))


def _tiny(attrs: JsonObject) -> bool:
    """A frame whose ``width`` or ``height`` attribute is at most a pixel."""
    return any(
        (size := _svg_number(as_str(attrs.get(name)))) is not None and size <= 1
        for name in ("width", "height")
    )


def _text_attrs(attrs: JsonObject) -> dict[str, str]:
    """The string attributes the cascade sees; a dialog the recording says is open has
    ``open`` (a modal one shown by script)."""
    found = {k: v for k, v in attrs.items() if isinstance(v, str)}
    if _DIALOG_OPEN_ATTR in attrs:
        found.setdefault("open", "")
    return found


def _is_editable(attrs: JsonObject) -> bool:
    editable = next((v for k, v in attrs.items() if k.lower() == "contenteditable"), None)
    return editable is not None and (as_str(editable) or "").strip().lower() != "false"


def _is_selected(attrs: JsonObject) -> bool:
    flag = as_str(attrs.get(_SELECTED_ATTR))
    if flag is not None:
        return flag != "false"
    return "selected" in attrs


def _normalise(text: str | None) -> str:
    return " ".join(text.split()) if text else ""


def _is_fixed(attrs: JsonObject) -> bool:
    """A ``readonly`` or ``disabled`` field: the agent cannot type into it."""
    return "readonly" in attrs or "disabled" in attrs


def _loads_content(attrs: JsonObject) -> bool:
    """A frame with a ``srcdoc`` or a ``src`` other than ``about:blank`` shows a document."""
    if as_str(attrs.get("srcdoc")):
        return True
    src = (as_str(attrs.get("src")) or "").strip().lower()
    return bool(src) and src != "about:blank"
