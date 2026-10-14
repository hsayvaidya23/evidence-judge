"""Decode Playwright frame snapshots (NodeSnapshot trees) into visible page text.

A snapshot's ``html`` is a NodeSnapshot: a text string, an element
``[name, attributes?, *children]``, or a subtree back-reference ``[[offset, index]]`` that
points at node ``index`` (post-order) of snapshot ``current - offset`` of the same frame.
This mirrors ``snapshotRenderer.ts`` in Playwright's isomorphic trace package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evidence_judge.ingest._json import JsonObject, as_int, as_list, as_object, as_str

_MAX_VISITS = 2_000_000

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


class SnapshotDecodeError(ValueError):
    """A snapshot's node tree is malformed (e.g. a self-referencing subtree)."""


@dataclass(frozen=True, slots=True)
class RenderedPage:
    title: str | None
    text: str


_BLOCK_END = object()


class FrameSnapshotStore:
    """Snapshot history of every frame, in recording order, for resolving back-references."""

    def __init__(self) -> None:
        self._roots: dict[str, list[object]] = {}
        self._nodes: dict[tuple[str, int], list[object]] = {}

    def add(self, frame_id: str, html: object) -> int:
        roots = self._roots.setdefault(frame_id, [])
        roots.append(html)
        return len(roots) - 1

    def root(self, frame_id: str, index: int) -> object:
        return self._roots[frame_id][index]

    def render(self, frame_id: str, index: int) -> RenderedPage:
        return _Renderer(self, frame_id).render(index)

    def resolve(self, frame_id: str, index: int, ref: object) -> tuple[object, int] | None:
        pair = as_list(ref)
        if len(pair) != 2:
            return None
        offset, node_index = as_int(pair[0]), as_int(pair[1])
        if offset is None or node_index is None:
            return None
        target = index - offset
        if not 0 <= target <= index:
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

    def render(self, index: int) -> RenderedPage:
        title: str | None = None
        out: list[str] = []
        stack: list[tuple[object, int]] = [(self._store.root(self._frame_id, index), index)]
        while stack:
            node, snapshot_index = stack.pop()
            self._tick()
            if node is _BLOCK_END:
                out.append(" ")
                continue
            resolved = self._deref(node, snapshot_index)
            if resolved is None:
                continue
            node, snapshot_index = resolved
            if isinstance(node, str):
                out.append(node)
                continue
            tag, attrs = _tag_and_attrs(node)
            if tag == "HEAD":
                title = title or self._title(node, snapshot_index)
                continue
            if _is_hidden(tag, attrs):
                continue
            if tag not in _INLINE_TAGS:
                out.append(" ")
                stack.append((_BLOCK_END, snapshot_index))
            control_text = self._control_text(node, tag, attrs, snapshot_index)
            if control_text is not None:
                out.append(control_text)
                continue
            stack.extend((child, snapshot_index) for child in reversed(_children(node)))
        return RenderedPage(title=_normalise(title) or None, text=_normalise("".join(out)))

    def _tick(self) -> None:
        self._visits += 1
        if self._visits > _MAX_VISITS:
            raise SnapshotDecodeError("snapshot node tree does not terminate")

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
            return as_str(attrs.get(_VALUE_ATTR)) or as_str(attrs.get("value")) or ""
        if tag == "TEXTAREA":
            value = as_str(attrs.get(_VALUE_ATTR))
            return value if value is not None else self._text_content(node, snapshot_index)
        if tag == "SELECT":
            return self._selected_option_text(node, snapshot_index)
        return None

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
    if tag == "DIALOG" and "open" not in attrs and _DIALOG_OPEN_ATTR not in attrs:
        return True
    style = "".join((as_str(attrs.get("style")) or "").split()).lower()
    return "display:none" in style or "visibility:hidden" in style


def _is_selected(attrs: JsonObject) -> bool:
    flag = as_str(attrs.get(_SELECTED_ATTR))
    if flag is not None:
        return flag != "false"
    return "selected" in attrs


def _normalise(text: str | None) -> str:
    return " ".join(text.split()) if text else ""
