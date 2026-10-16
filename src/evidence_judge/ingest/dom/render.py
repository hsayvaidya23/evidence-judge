"""Rendering one snapshot into page text: the tree walk, tree scopes and their stylesheets,
slots, form controls and frames."""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urljoin, urlsplit

from evidence_judge.core.site import registrable_domain
from evidence_judge.ingest._json import JsonObject, as_list, as_str
from evidence_judge.ingest.css import (
    FAINT_ALPHA,
    HIDDEN,
    REMOVED,
    ROOT,
    SHOWN,
    SVG,
    UNSURE,
    Box,
    Computed,
    Element,
    Sheet,
    StyleSheets,
    StylesheetSource,
    Viewport,
    media_holds,
)
from evidence_judge.ingest.dom.model import (
    BLOCK_END,
    Child,
    Item,
    RenderedPage,
    Scope,
    Shadow,
    SnapshotDecodeError,
    Visit,
)
from evidence_judge.ingest.dom.nodes import (
    FRAMES,
    INLINE_TAGS,
    SHADOW_ROOT_ATTR,
    SKIPPED_TAGS,
    as_element,
    child_nodes,
    child_styles,
    is_editable,
    is_fixed,
    is_selected,
    is_shadow_root,
    loads_content,
    namespace,
    new_element,
    normalise,
    off_canvas,
    tag_and_attrs,
    text_attrs,
    tiny,
)

if TYPE_CHECKING:
    from evidence_judge.ingest.dom.store import FrameSnapshotStore

_FRAGMENT_REF = re.compile(r"""url\(\s*['"]?#([^'")\s]+)""")

_NON_TEXT_INPUTS = frozenset({"hidden", "password", "file", "checkbox", "radio", "image"})
_BUTTON_INPUTS = frozenset({"submit", "button", "reset"})

_VALUE_ATTR = "__playwright_value_"

_RECORDED_FRAME = re.compile(r"^/snapshot/(frame@[^/?#]+)")
"""The ``src`` Playwright gives a frame it recorded: its frame id."""
_STYLE_SHEET_ATTR = "__playwright_style_sheet_"
"""The text of a constructed (adopted) stylesheet, or of one a script changed."""
MAX_SNAPSHOT_CSS: Final = 8 * 1024 * 1024
"""How many characters of stylesheets one scope of a snapshot applies; beyond that its styles
are unknown."""


class Renderer:
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
        self._pieces: list[tuple[int, tuple[Box, ...]]] = []
        """Where each piece of shown text is in the shown text, and its positioned ancestors."""
        self._order: dict[int, int] = {}
        """The tree order of each positioned element, by its id."""
        self._overlays: list[tuple[Box, Box | None, frozenset[int]]] = []
        """Each box that may paint over the page: it, its containing block (None for the
        viewport) and the ids of its positioned ancestors."""
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
        stack: list[Item] = []
        if resolved is not None:
            node, at = resolved
            element = None if isinstance(node, str) else new_element(node, None)
            stack.append((node, at, element, start, False, scope))
        while stack:
            node, snapshot_index, element, inherited, folded, scope = stack.pop()
            self._tick()
            if node is BLOCK_END:
                self._emit(" ", None)
                continue
            if isinstance(node, str):
                if not folded:
                    self._emit(node, inherited.text, inherited.boxes)
                continue
            node = as_element(node)
            if element is None or node is None:
                continue
            tag, attrs = tag_and_attrs(node)
            if tag == "HEAD":
                title = title or self._title(node, snapshot_index)
                continue
            if folded and tag != "SUMMARY":
                if not scope.sheets.details:
                    continue
                inherited, folded = replace(inherited, unsure=True), False
            visit = Visit(node, snapshot_index, element, inherited, scope, tag, attrs)
            self._element(visit, stack)
        if self._overlays:
            self._cover()
        return RenderedPage(
            title=normalise(title) or None,
            text=normalise("".join(self._shown)),
            form_values=len(self._field_values),
            field_values=tuple(self._field_values),
            unsure_text=normalise("".join(self._unsure)),
            opaque=self._opaque,
        )

    def unstyled(self, index: int) -> RenderedPage:
        """The snapshot's text read without its styles, all of it unsure, for a snapshot whose
        styles the reader could not apply: the page may show any of it, or text it never saw."""
        root = self._store.root(self._frame_id, index)
        title: str | None = None
        stack: list[tuple[object, int]] = [(root, index)]
        while stack:
            node, snapshot_index = stack.pop()
            self._tick()
            if node is BLOCK_END:
                self._emit(" ", None)
                continue
            resolved = self._deref(node, snapshot_index)
            if resolved is None:
                continue
            target, at = resolved
            if isinstance(target, str):
                self._emit(target, UNSURE)
                continue
            tag, attrs = tag_and_attrs(target)
            if tag == "HEAD":
                title = title or self._title(target, at)
                continue
            if tag in SKIPPED_TAGS or (tag == "TEMPLATE" and SHADOW_ROOT_ATTR not in attrs):
                continue
            if tag not in INLINE_TAGS:
                self._emit(" ", None)
                stack.append((BLOCK_END, at))
            stack.extend((child, at) for child in reversed(child_nodes(target)))
        return RenderedPage(
            title=normalise(title) or None,
            text="",
            unsure_text=normalise("".join(self._unsure)),
            opaque=True,
        )

    def _element(self, visit: Visit, stack: list[Item]) -> None:
        tag, attrs, element, scope = visit.tag, visit.attrs, visit.element, visit.scope
        if tag in SKIPPED_TAGS or (tag == "TEMPLATE" and SHADOW_ROOT_ATTR not in attrs):
            if tag in FRAMES and not self._opaque:
                self._opaque = self._frame_shows(element, visit.inherited, scope, attrs)
            return
        children = self._resolved(visit.node, visit.index, parent=element)
        shadow = self._shadow_root(visit, children)
        style = self._style(visit, shadow)
        if style.removed:
            return
        if style.boxes and style.boxes[-1][0] is element:
            self._positioned(style, scope)
        if ("-" in tag and not children) or (
            style.text != HIDDEN and scope.sheets.generates(element)
        ):
            self._opaque = True
        if tag not in INLINE_TAGS:
            self._emit(" ", None)
            stack.append((BLOCK_END, visit.index, None, style, False, scope))
        if self._shows_control(visit, style):
            return
        closed = tag == "DETAILS" and "open" not in attrs
        if shadow is not None:
            self._push_shadow(shadow, style, closed, stack)
            return
        if self._push_slotted(visit, style, stack):
            return
        stack.extend(
            (c.node, c.index, c.element, s, closed, scope)
            for c, s in reversed(
                list(zip(children, child_styles(element, style, children), strict=True))
            )
        )

    def _shadow_root(self, visit: Visit, children: list[Child]) -> Shadow | None:
        """The element's declarative shadow root, if it has one."""
        shadow = next(
            (
                c
                for c in children
                if c.element is not None
                and c.element.tag == "TEMPLATE"
                and SHADOW_ROOT_ATTR in c.element.attrs
            ),
            None,
        )
        if shadow is None or not isinstance(shadow.node, list):
            return None
        proxy = Element(visit.tag, visit.element.attrs, None, host=visit.element)
        inner = self._shadow_scope(shadow, children, visit.scope)
        return Shadow(shadow.node, shadow.index, inner, proxy)

    def _style(self, visit: Visit, shadow: Shadow | None) -> Computed:
        element, inherited, scope = visit.element, visit.inherited, visit.scope
        host = shadow.scope.sheets if shadow is not None else None
        proxy = shadow.proxy if shadow is not None else None
        style = scope.sheets.style(element, inherited, host=host, proxy=proxy)
        if style.removed and element.namespace == SVG and self._referenced:
            style = self._copied(visit.node, visit.index, element, inherited, scope, host, proxy)
        if style.removed:
            return style
        if element.namespace == SVG and visit.tag == "TEXT" and off_canvas(element, style):
            style = replace(style, unsure=True)
        if scope.parts and "part" in element.attrs and not style.unsure:
            style = replace(style, unsure=True)
        return style

    def _shows_control(self, visit: Visit, style: Computed) -> bool:
        """Emit a form control's text, which stands for its children; False if not a control."""
        control_text = self._control_text(visit.node, visit.tag, visit.attrs, visit.index)
        if control_text is None:
            return False
        self._emit(control_text, style.text, style.boxes)
        if self._fixed:
            self._emit(f" {self._fixed} ", max(style.text, UNSURE))
            self._fixed = ""
        return True

    def _push_shadow(
        self, shadow: Shadow, style: Computed, closed: bool, stack: list[Item]
    ) -> None:
        start = style if not shadow.scope.unknown else replace(style, unsure=True)
        rooted = self._resolved(shadow.node, shadow.index, parent=shadow.proxy)
        stack.extend(
            (c.node, c.index, c.element, start, closed, shadow.scope) for c in reversed(rooted)
        )

    def _push_slotted(self, visit: Visit, style: Computed, stack: list[Item]) -> bool:
        """Push the light children a slot shows, styled in the host's scope; False if none."""
        scope = visit.scope
        assigned = self._assigned(visit.node, visit.attrs, scope) if visit.tag == "SLOT" else None
        if not assigned or scope.outer is None:
            return False
        passed = style if not scope.sheets.slotted else replace(style, unsure=True)
        stack.extend(
            (c.node, c.index, c.element, passed, False, scope.outer) for c in reversed(assigned)
        )
        return True

    def _frame_shows(
        self, element: Element, inherited: Computed, scope: Scope, attrs: JsonObject
    ) -> bool:
        """A frame may show text of the site's own that this snapshot does not hold: it loads
        a document of the same site into a box bigger than a pixel that is not off-screen. A
        third-party frame (a payment field, a video, an analytics beacon) holds the third
        party's page, not the site's."""
        if not loads_content(attrs) or tiny(attrs) or not self._same_site(attrs):
            return False
        style = scope.sheets.style(element, inherited)
        return not style.removed and style.text != HIDDEN and not scope.sheets.collapsed(element)

    def _same_site(self, attrs: JsonObject) -> bool:
        """The frame may hold a page of this page's site: its ``srcdoc``, or a URL of the same
        registrable domain (for a frame Playwright recorded, any URL it had)."""
        src = as_str(attrs.get("src")) or ""
        if as_str(attrs.get("srcdoc")) or self._url is None:
            return True
        if (recorded := _RECORDED_FRAME.match(src)) is not None:
            urls = self._store.frame_urls.get(recorded.group(1))
            return not urls or any(self._same_site_url(u) for u in urls)
        return self._same_site_url(urljoin(self._document or self._url, src))

    def _same_site_url(self, url: str) -> bool:
        try:
            frame, page = urlsplit(url), urlsplit(self._url or "")
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
        scope: Scope,
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
            target = None if resolved is None else as_element(resolved[0])
            if resolved is None or target is None:
                continue
            if as_str(tag_and_attrs(target)[1].get("id")) in self._referenced:
                return True
            stack.extend((c, resolved[1]) for c in child_nodes(target))
        return False

    @staticmethod
    def _assigned(node: list[Any], attrs: JsonObject, scope: Scope) -> list[Child] | None:
        """The light children a slot shows, if it is the first of its name in a shadow tree."""
        if scope.slots is None or id(node) not in scope.first_slots:
            return None
        return scope.slots.get(as_str(attrs.get("name")) or "")

    def _positioned(self, style: Computed, scope: Scope) -> None:
        """Note a positioned element's tree order, and whether it may paint over the page: an
        opaque box over the viewport (or its containing block) that the page does not hide."""
        box = style.boxes[-1]
        element, z, position = box
        self._order[id(element)] = len(self._order)
        if position not in ("absolute", "fixed") or (z is not None and z < 0):
            return
        if style.visibility == HIDDEN or style.alpha <= FAINT_ALPHA:
            return
        if not scope.sheets.covers(element):
            return
        outer = style.boxes[:-1]
        block = outer[-1] if position == "absolute" and outer else None
        ancestors = frozenset(id(e) for e, _, _ in outer)
        self._overlays.append((box, block, ancestors))

    def _cover(self) -> None:
        """Move the shown text an opaque box paints over to the unsure text: text outside it,
        in its containing block, that no positioned ancestor of its own lifts above it (a higher
        ``z-index``, or the same one later in tree order)."""
        for index, boxes in self._pieces:
            if not any(self._under(overlay, boxes) for overlay in self._overlays):
                continue
            self._unsure.extend((self._shown[index], " "))
            self._shown[index] = " "

    def _under(
        self, overlay: tuple[Box, Box | None, frozenset[int]], boxes: tuple[Box, ...]
    ) -> bool:
        (element, z, _), block, ancestors = overlay
        ids = {id(e) for e, _, _ in boxes}
        if id(element) in ids or (block is not None and id(block[0]) not in ids):
            return False
        top = float("inf") if z is None else z
        after = self._order[id(element)]
        return not any(
            own is not None and (own > top or (own == top and self._order.get(id(e), -1) > after))
            for e, own, _ in boxes
            if id(e) not in ancestors
        )

    def _emit(self, text: str, state: int | None, boxes: tuple[Box, ...] = ()) -> None:
        """Add `text` to the shown or unsure text; a block break (state None) to both."""
        if state is None:
            self._shown.append(text)
            self._unsure.append(text)
        elif state == SHOWN:
            self._pieces.append((len(self._shown), boxes))
            self._shown.append(text)
            if self._unsure and self._unsure[-1] != " ":
                self._unsure.append(" ")
        elif state != HIDDEN:
            self._unsure.append(text)
            if self._shown and self._shown[-1] != " ":
                self._shown.append(" ")

    def _resolved(
        self, node: list[Any], snapshot_index: int, *, parent: Element | None = None
    ) -> list[Child]:
        """The children of `node`, resolved; elements get the Element the cascade sees, with
        `parent` and their element siblings (a shadow root and constructed sheets are not
        among them)."""
        out: list[Child] = []
        siblings: list[Element] = []
        for child in child_nodes(node):
            resolved = self._deref(child, snapshot_index)
            if resolved is None:
                continue
            target, index = resolved
            if isinstance(target, str):
                out.append(Child(target, index, None))
                continue
            tag, attrs = tag_and_attrs(target)
            element = Element(tag, text_attrs(attrs), parent, namespace=namespace(tag, parent))
            if tag != "TEMPLATE" or (
                SHADOW_ROOT_ATTR not in attrs and _STYLE_SHEET_ATTR not in attrs
            ):
                element.siblings, element.index = siblings, len(siblings)
                siblings.append(element)
            out.append(Child(target, index, element))
        return out

    def _shadow_scope(self, shadow: Child, children: list[Child], outer: Scope) -> Scope:
        """The scope of a shadow root, with the host's light children (`children`) by slot."""
        root = shadow.node
        assert isinstance(root, list)
        sheets, unknown = self._scope_sheets([(c, shadow.index) for c in child_nodes(root)])
        styles = self._store.style_sheets(sheets, self._viewport, self._stylesheets, document=False)
        slots: dict[str, list[Child]] = {}
        for child in children:
            if child is shadow:
                continue
            name = "" if child.element is None else child.element.attrs.get("slot", "")
            slots.setdefault(name, []).append(child)
        return Scope(
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
        stack: list[tuple[object, int]] = [(c, snapshot_index) for c in reversed(child_nodes(root))]
        while stack:
            node, index = stack.pop()
            self._tick()
            resolved = self._deref(node, index)
            target = None if resolved is None else as_element(resolved[0])
            if resolved is None or target is None:
                continue
            at = resolved[1]
            if is_shadow_root(target):
                continue
            tag, attrs = tag_and_attrs(target)
            if tag == "SLOT":
                seen.setdefault(as_str(attrs.get("name")) or "", id(target))
            stack.extend((c, at) for c in reversed(child_nodes(target)))
        return frozenset(seen.values())

    def _scope(self, roots: list[tuple[object, int]], *, document: bool) -> Scope:
        sheets, unknown = self._scope_sheets(roots)
        styles = self._store.style_sheets(
            sheets, self._viewport, self._stylesheets, document=document
        )
        return Scope(styles, unknown or styles.unknown)

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
            tag, attrs = tag_and_attrs(element)
            if is_shadow_root(element):
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
                stack.extend((child, snapshot_index) for child in reversed(child_nodes(element)))
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
            element = None if resolved is None else as_element(resolved[0])
            if resolved is None or element is None:
                continue
            at = resolved[1]
            tag, attrs = tag_and_attrs(element)
            if tag == "BASE" and (href := as_str(attrs.get("href"))) is not None:
                return href
            if tag in ("HTML", "HEAD"):
                stack.extend((c, at) for c in reversed(child_nodes(element)))
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
            element = as_element(node)
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
            (child, snapshot_index) for child in reversed(child_nodes(node))
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
                stack.extend((c, target_index) for c in reversed(child_nodes(target)))
        return "".join(parts)

    def _resolved_children(
        self, node: list[Any], snapshot_index: int
    ) -> list[tuple[list[Any], int]]:
        resolved: list[tuple[list[Any], int]] = []
        for child in child_nodes(node):
            target = self._deref(child, snapshot_index)
            if target is not None and not isinstance(target[0], str):
                resolved.append((target[0], target[1]))
        return resolved

    def _title(self, head: list[Any], snapshot_index: int) -> str | None:
        for child, index in self._resolved_children(head, snapshot_index):
            if tag_and_attrs(child)[0] == "TITLE":
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
            return self._excluded_value(value, fixed=is_fixed(attrs))
        if tag == "TEXTAREA":
            value = as_str(attrs.get(_VALUE_ATTR))
            if value is None:
                value = self._text_content(node, snapshot_index)
            return self._excluded_value(value, fixed=is_fixed(attrs))
        if tag == "SELECT":
            return self._excluded_value(self._selected_option_text(node, snapshot_index))
        if is_editable(attrs):
            return self._excluded_value(self._text_content(node, snapshot_index))
        return None

    def _excluded_value(self, value: str | None, *, fixed: bool = False) -> str:
        """Keep a field's value out of the text, as a field value. A `fixed` field's value (one
        the agent cannot type into) is text the page may show: it is the site's, but it is a
        field value too, so it never proves a predicate; it only keeps a phrase's absence from
        contradicting one."""
        if value := normalise(value):
            self._field_values.append(value)
            if fixed:
                self._fixed = value
        return ""

    def _selected_option_text(self, select: list[Any], snapshot_index: int) -> str:
        options: list[tuple[list[Any], int]] = []
        for child, index in self._resolved_children(select, snapshot_index):
            tag = tag_and_attrs(child)[0]
            if tag == "OPTION":
                options.append((child, index))
            elif tag == "OPTGROUP":
                options.extend(
                    (opt, i)
                    for opt, i in self._resolved_children(child, index)
                    if tag_and_attrs(opt)[0] == "OPTION"
                )
        if not options:
            return ""
        chosen = [o for o in options if is_selected(tag_and_attrs(o[0])[1])] or options[:1]
        return " ".join(self._text_content(option, index) for option, index in chosen)
