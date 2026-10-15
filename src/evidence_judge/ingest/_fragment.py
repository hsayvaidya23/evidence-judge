"""Page text of an HTML fragment, by the rules a snapshot's page text follows (`_dom`)."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Final

from evidence_judge.ingest._dom import FrameSnapshotStore

_VOID: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_FRAME: Final = "fragment"


class _TreeBuilder(HTMLParser):
    """Builds a NodeSnapshot tree (`[NAME, attributes, *children]`) from HTML. Comments are
    dropped; an end tag closes the innermost open element of its name and those inside it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: list[object] = ["BODY", {}]
        self._open: list[list[object]] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node: list[object] = [tag.upper(), {name: value or "" for name, value in attrs}]
        self._open[-1].append(node)
        if tag not in _VOID:
            self._open.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open[-1].append([tag.upper(), {name: value or "" for name, value in attrs}])

    def handle_endtag(self, tag: str) -> None:
        name = tag.upper()
        for at in range(len(self._open) - 1, 0, -1):
            if self._open[at][0] == name:
                del self._open[at:]
                return

    def handle_data(self, data: str) -> None:
        self._open[-1].append(data)


def fragment_text(html: str) -> str:
    """The text a page that inserts `html` into its markup shows: comments, scripts, styles,
    templates and what the fragment hides (the ``hidden`` attribute, inline or ``<style>``
    ``display: none``, hiding utility classes) left out, as in a snapshot's page text."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    store = FrameSnapshotStore()
    return store.render(_FRAME, store.add(_FRAME, builder.root)).text
