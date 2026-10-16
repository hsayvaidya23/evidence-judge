"""`FrameSnapshotStore`: every frame's snapshot history, back-references and parsed
stylesheets."""

from __future__ import annotations

from typing import Final

from evidence_judge.ingest._json import as_int, as_list
from evidence_judge.ingest.css import Sheet, StyleSheets, StylesheetSource, Viewport
from evidence_judge.ingest.dom.model import RenderedPage, SnapshotDecodeError
from evidence_judge.ingest.dom.nodes import as_element, child_nodes
from evidence_judge.ingest.dom.render import Renderer

DEFAULT_MAX_VISITS = 1_000_000

_CACHED_SHEETS: Final = 16
"""How many parsed stylesheet sets a store keeps (a run's pages share a few)."""


class FrameSnapshotStore:
    """Snapshot history of every frame, in recording order, for resolving back-references."""

    def __init__(self, *, max_visits: int = DEFAULT_MAX_VISITS) -> None:
        self.max_visits = max_visits
        self._roots: dict[str, list[object]] = {}
        self.frame_urls: dict[str, set[str]] = {}
        """Every URL each frame's snapshots were taken at."""
        self._nodes: dict[tuple[str, int], list[object]] = {}
        self._sheets: dict[tuple[tuple[Sheet, ...], Viewport | None, bool], StyleSheets] = {}

    def add(self, frame_id: str, html: object, *, url: str | None = None) -> int:
        if url:
            self.frame_urls.setdefault(frame_id, set()).add(url)
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
        ``@media`` rules and `stylesheets` supplies linked and imported stylesheets. A snapshot
        whose styles the reader fails on (input built to exhaust it) is read without them: all
        its text unsure, and the page opaque."""
        try:
            return Renderer(self, frame_id, viewport, stylesheets, url).render(index)
        except SnapshotDecodeError:
            raise
        except (RecursionError, ArithmeticError, ValueError):
            return Renderer(self, frame_id, viewport, None, url).unstyled(index)

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
            elif as_element(node) is not None:
                if expanded:
                    nodes.append(node)
                else:
                    stack.append((node, True))
                    stack.extend((child, False) for child in reversed(child_nodes(node)))
        self._nodes[key] = nodes
        return nodes
