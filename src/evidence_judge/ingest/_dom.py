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

Text the snapshot shows hidden is not page text either: elements with the ``hidden`` attribute,
a closed ``<dialog>``, the content of a closed ``<details>`` (its ``<summary>`` stays), and what
the page's styles hide with ``display: none``, ``visibility: hidden`` or
``content-visibility: hidden`` (`_css`): inline styles, ``<style>`` elements, constructed
stylesheets and ``<link rel=stylesheet>`` sheets the recording holds, and common utility classes
(``hidden``, ``d-none``, ``sr-only``, ...). Text hidden by a stylesheet the recording does not
hold, by script, by layout (off-screen, zero size, clipped, covered) or by colour still counts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urljoin

from evidence_judge.ingest._css import Element, StyleSheets, media_matches
from evidence_judge.ingest._json import JsonObject, as_int, as_list, as_object, as_str

DEFAULT_MAX_VISITS = 1_000_000

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
    }
)
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
    }
)
_NON_TEXT_INPUTS = frozenset({"hidden", "password", "file", "checkbox", "radio", "image"})
_BUTTON_INPUTS = frozenset({"submit", "button", "reset"})
_SHADOW_ROOT_ATTR = "__playwright_shadow_root_"
_VALUE_ATTR = "__playwright_value_"
_SELECTED_ATTR = "__playwright_selected_"
_DIALOG_OPEN_ATTR = "__playwright_dialog_open_"
_STYLE_SHEET_ATTR = "__playwright_style_sheet_"
"""The text of a constructed (adopted) stylesheet, or of one a script changed."""
MAX_SNAPSHOT_CSS: Final = 4 * 1024 * 1024
"""How many characters of stylesheets one snapshot applies; later sheets are left out."""

_CACHED_SHEETS: Final = 16
"""How many parsed stylesheet sets a store keeps (a run's pages share a few)."""

StylesheetSource = Callable[[str], str | None]
"""The text of the stylesheet at an absolute URL, as the recording holds it, or None."""
Viewport = tuple[int, int]


class SnapshotDecodeError(ValueError):
    """A snapshot's node tree expands beyond the visit budget (malformed or hostile input)."""


@dataclass(frozen=True, slots=True)
class RenderedPage:
    title: str | None
    text: str
    form_values: int = 0
    """Non-empty text-field, textarea, editable-element and selected-option values left out of
    ``text``."""


_BLOCK_END = object()
_Item = tuple[object, int, Element | None, bool, bool]
"""A node to render: the node, its snapshot, its parent element, whether ``visibility`` hides
it, and whether it is inside a closed ``<details>`` (only a ``<summary>`` shows)."""


class FrameSnapshotStore:
    """Snapshot history of every frame, in recording order, for resolving back-references."""

    def __init__(self, *, max_visits: int = DEFAULT_MAX_VISITS) -> None:
        self.max_visits = max_visits
        self._roots: dict[str, list[object]] = {}
        self._nodes: dict[tuple[str, int], list[object]] = {}
        self._sheets: dict[tuple[tuple[str, ...], Viewport | None], StyleSheets] = {}

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
    ) -> RenderedPage:
        """Page text of snapshot `index` of `frame_id`; `viewport` evaluates ``@media`` rules
        and `stylesheets` supplies linked stylesheets."""
        return _Renderer(self, frame_id).render(index, viewport, stylesheets)

    def style_sheets(self, sheets: tuple[str, ...], viewport: Viewport | None) -> StyleSheets:
        key = (sheets, viewport)
        cached = self._sheets.get(key)
        if cached is None:
            if len(self._sheets) >= _CACHED_SHEETS:
                del self._sheets[next(iter(self._sheets))]
            cached = self._sheets[key] = StyleSheets(sheets, viewport)
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
    def __init__(self, store: FrameSnapshotStore, frame_id: str) -> None:
        self._store = store
        self._frame_id = frame_id
        self._visits = 0
        self._form_values = 0

    def render(
        self, index: int, viewport: Viewport | None, stylesheets: StylesheetSource | None
    ) -> RenderedPage:
        sheets = self._store.style_sheets(self._sheets(index, viewport, stylesheets), viewport)
        title: str | None = None
        out: list[str] = []
        root = self._store.root(self._frame_id, index)
        stack: list[_Item] = [(root, index, None, False, False)]
        while stack:
            node, snapshot_index, parent, invisible, folded = stack.pop()
            self._tick()
            if node is _BLOCK_END:
                out.append(" ")
                continue
            resolved = self._deref(node, snapshot_index)
            if resolved is None:
                continue
            node, snapshot_index = resolved
            if isinstance(node, str):
                if not (invisible or folded):
                    out.append(node)
                continue
            tag, attrs = _tag_and_attrs(node)
            if tag == "HEAD":
                title = title or self._title(node, snapshot_index)
                continue
            if (folded and tag != "SUMMARY") or _is_hidden(tag, attrs):
                continue
            element = Element(tag, _text_attrs(attrs), parent)
            style = sheets.style(element)
            if style.removed:
                continue
            hidden = invisible if style.invisible is None else style.invisible
            if tag not in _INLINE_TAGS:
                out.append(" ")
                stack.append((_BLOCK_END, snapshot_index, None, False, False))
            control_text = self._control_text(node, tag, attrs, snapshot_index)
            if control_text is not None:
                if not hidden:
                    out.append(control_text)
                continue
            closed = tag == "DETAILS" and "open" not in attrs
            stack.extend(
                (child, snapshot_index, element, hidden, closed)
                for child in reversed(_children(node))
            )
        return RenderedPage(
            title=_normalise(title) or None,
            text=_normalise("".join(out)),
            form_values=self._form_values,
        )

    def _sheets(
        self, index: int, viewport: Viewport | None, stylesheets: StylesheetSource | None
    ) -> tuple[str, ...]:
        """The stylesheets snapshot `index` applies, in document order: ``<style>`` text,
        constructed sheets, and the linked sheets `stylesheets` holds, each when its ``media``
        holds at `viewport`; at most MAX_SNAPSHOT_CSS characters in all."""
        found: list[str] = []
        base: str | None = None
        size = 0
        stack: list[tuple[object, int]] = [(self._store.root(self._frame_id, index), index)]
        while stack and size < MAX_SNAPSHOT_CSS:
            node, snapshot_index = stack.pop()
            self._tick()
            resolved = self._deref(node, snapshot_index)
            if resolved is None:
                continue
            element, snapshot_index = resolved
            if isinstance(element, str):
                continue
            tag, attrs = _tag_and_attrs(element)
            if tag == "BASE" and base is None:
                base = as_str(attrs.get("href"))
            text = self._sheet(element, tag, attrs, snapshot_index, base, stylesheets)
            media = as_str(attrs.get("media"))
            if text and (media is None or media_matches(media, viewport)):
                found.append(text[: MAX_SNAPSHOT_CSS - size])
                size += len(found[-1])
            if tag not in ("STYLE", "SCRIPT", "NOSCRIPT"):
                stack.extend((child, snapshot_index) for child in reversed(_children(element)))
        return tuple(found)

    def _sheet(
        self,
        node: list[Any],
        tag: str,
        attrs: JsonObject,
        snapshot_index: int,
        base: str | None,
        stylesheets: StylesheetSource | None,
    ) -> str | None:
        if (constructed := as_str(attrs.get(_STYLE_SHEET_ATTR))) is not None:
            return constructed
        if tag == "STYLE":
            return self._text_content(node, snapshot_index)
        rel = (as_str(attrs.get("rel")) or "").lower().split()
        href = as_str(attrs.get("href"))
        if tag != "LINK" or "stylesheet" not in rel or "alternate" in rel or href is None:
            return None
        if "disabled" in attrs or stylesheets is None:
            return None
        try:
            url = urljoin(base or "", href)
        except ValueError:
            return None
        return stylesheets(url)

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
            return self._excluded_value(value)
        if tag == "TEXTAREA":
            value = as_str(attrs.get(_VALUE_ATTR))
            if value is None:
                value = self._text_content(node, snapshot_index)
            return self._excluded_value(value)
        if tag == "SELECT":
            return self._excluded_value(self._selected_option_text(node, snapshot_index))
        if _is_editable(attrs):
            return self._excluded_value(self._text_content(node, snapshot_index))
        return None

    def _excluded_value(self, value: str | None) -> str:
        if value and value.strip():
            self._form_values += 1
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


def _is_hidden(tag: str, attrs: JsonObject) -> bool:
    if tag in _SKIPPED_TAGS:
        return True
    if tag == "TEMPLATE":
        return _SHADOW_ROOT_ATTR not in attrs
    if "hidden" in attrs:
        return True
    return tag == "DIALOG" and "open" not in attrs and _DIALOG_OPEN_ATTR not in attrs


def _text_attrs(attrs: JsonObject) -> dict[str, str]:
    return {k: v for k, v in attrs.items() if isinstance(v, str)}


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
