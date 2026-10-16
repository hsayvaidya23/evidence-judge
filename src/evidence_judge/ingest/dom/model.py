"""What rendering a snapshot yields, and the nodes, scopes and shadow roots it works with."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evidence_judge.ingest._json import JsonObject
from evidence_judge.ingest.css import Computed, Element, StyleSheets


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


BLOCK_END = object()


@dataclass(frozen=True, slots=True)
class Child:
    """A child node, resolved, with the element the cascade sees for it (None for text)."""

    node: str | list[Any]
    index: int
    element: Element | None


@dataclass(frozen=True, slots=True)
class Scope:
    """A tree scope: the document, or one shadow root."""

    sheets: StyleSheets
    unknown: bool
    """Its styles are partly unknown: every element in it is unsure."""
    outer: Scope | None = None
    """For a shadow root: the scope of its host, where the host's light children are styled."""
    slots: dict[str, list[Child]] | None = None
    """For a shadow root: the host's light children by the slot name they ask for."""
    first_slots: frozenset[int] = frozenset()
    """The ids of the slot elements that get assigned children (the first of each name)."""
    parts: bool = False
    """An enclosing scope has ``::part()`` rules that may hide elements of this one."""


@dataclass(frozen=True, slots=True)
class Visit:
    """An element being rendered: its node and snapshot, the element the cascade sees, what
    its parent decided, its tree scope, and its tag and attributes."""

    node: list[Any]
    index: int
    element: Element
    inherited: Computed
    scope: Scope
    tag: str
    attrs: JsonObject


@dataclass(frozen=True, slots=True)
class Shadow:
    """An element's declarative shadow root: its template node and snapshot, the scope inside
    it, and the element that stands for the host in the root's own rules."""

    node: list[Any]
    index: int
    scope: Scope
    proxy: Element


Item = tuple[object, int, Element | None, Computed, bool, Scope]
"""A node to render: the node, its snapshot, the element the cascade sees for it, what its
parent decided, whether it is inside a closed ``<details>`` (only a ``<summary>`` shows), and its
tree scope."""
