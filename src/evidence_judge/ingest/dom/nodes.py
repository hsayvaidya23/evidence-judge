"""NodeSnapshot nodes: their shape, tags and attributes, and what an element's tag and
attributes say about the text it shows."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Final, cast

from evidence_judge.ingest._json import JsonObject, as_list, as_object, as_str
from evidence_judge.ingest.css import HIDDEN, HTML, MATHML, SVG, Computed, Element
from evidence_judge.ingest.dom.model import Child

FRAMES = frozenset({"IFRAME", "FRAME"})
"""Elements that show another document, which the snapshot of this one does not hold."""
SKIPPED_TAGS = frozenset(
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
INLINE_TAGS = frozenset(
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

_NUMBER = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+))(?:px)?(?:[\s,]|$)")

SHADOW_ROOT_ATTR = "__playwright_shadow_root_"

_SELECTED_ATTR = "__playwright_selected_"
_DIALOG_OPEN_ATTR = "__playwright_dialog_open_"
_RECT_ATTR = "__playwright_bounding_rect__"


def as_element(node: object) -> list[Any] | None:
    items = as_list(node)
    return items if items and isinstance(items[0], str) else None


def child_nodes(node: object) -> list[Any]:
    return as_list(node)[2:]


def tag_and_attrs(node: list[Any]) -> tuple[str, JsonObject]:
    attrs = as_object(node[1]) if len(node) > 1 else {}
    return str(node[0]).upper(), attrs


def new_element(node: list[Any], parent: Element | None) -> Element:
    tag, attrs = tag_and_attrs(node)
    return Element(tag, text_attrs(attrs), parent)


def is_shadow_root(node: object) -> bool:
    element = as_element(node)
    if element is None:
        return False
    tag, attrs = tag_and_attrs(element)
    return tag == "TEMPLATE" and SHADOW_ROOT_ATTR in attrs


def namespace(tag: str, parent: Element | None) -> str:
    """The namespace an element with `tag` under `parent` is in, as an HTML parser decides."""
    above = HTML if parent is None else parent.namespace
    if above == SVG and parent is not None and parent.tag != "FOREIGNOBJECT":
        return SVG
    if above == MATHML and parent is not None and parent.tag not in _MATH_TEXT:
        return MATHML
    return {"SVG": SVG, "MATH": MATHML}.get(tag, HTML)


def child_styles(element: Element, style: Computed, children: list[Child]) -> list[Computed]:
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


def off_canvas(text: Element, style: Computed) -> bool:
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


def tiny(attrs: JsonObject) -> bool:
    """A frame whose ``width`` or ``height`` attribute is at most a pixel, or whose box as
    the recording measured it is, or lies above or left of the page."""
    if any(
        (size := _svg_number(as_str(attrs.get(name)))) is not None and size <= 1
        for name in ("width", "height")
    ):
        return True
    try:
        rect = json.loads(as_str(attrs.get(_RECT_ATTR)) or "null")
    except json.JSONDecodeError:
        return False
    if not isinstance(rect, dict):
        return False
    box = cast(dict[str, object], rect)
    left, top, right, bottom = (box.get(k) for k in ("left", "top", "right", "bottom"))
    if not all(isinstance(v, int | float) for v in (left, top, right, bottom)):
        return False
    left, top, right, bottom = (float(cast(float, v)) for v in (left, top, right, bottom))
    return right - left <= 1 or bottom - top <= 1 or right <= 0 or bottom <= 0


def text_attrs(attrs: JsonObject) -> dict[str, str]:
    """The string attributes the cascade sees; a dialog the recording says is open has
    ``open`` (a modal one shown by script)."""
    found = {k: v for k, v in attrs.items() if isinstance(v, str)}
    if _DIALOG_OPEN_ATTR in attrs:
        found.setdefault("open", "")
    return found


def is_editable(attrs: JsonObject) -> bool:
    editable = next((v for k, v in attrs.items() if k.lower() == "contenteditable"), None)
    return editable is not None and (as_str(editable) or "").strip().lower() != "false"


def is_selected(attrs: JsonObject) -> bool:
    flag = as_str(attrs.get(_SELECTED_ATTR))
    if flag is not None:
        return flag != "false"
    return "selected" in attrs


def normalise(text: str | None) -> str:
    return " ".join(text.split()) if text else ""


def is_fixed(attrs: JsonObject) -> bool:
    """A ``readonly`` or ``disabled`` field: the agent cannot type into it."""
    return "readonly" in attrs or "disabled" in attrs


def loads_content(attrs: JsonObject) -> bool:
    """A frame with a ``srcdoc`` or a ``src`` other than ``about:blank`` shows a document."""
    if as_str(attrs.get("srcdoc")):
        return True
    src = (as_str(attrs.get("src")) or "").strip().lower()
    return bool(src) and src != "about:blank"
