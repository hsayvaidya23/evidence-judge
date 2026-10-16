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
(`css`). Hidden: ``<script>``, ``<style>``, ``<template>`` and the like, the body of a closed
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
reader does not model (see `css`), including a ``popover`` (snapshots do not record whether it
is open), the content of a never-rendered SVG element something refers to (a ``<use>`` copy, a
pattern fill, a mask), a ``<switch>`` child other than an unconditional first one, and SVG text
placed past its canvas; and all text of a scope whose stylesheets the recording does not fully
hold. An empty custom element may hold a closed shadow root the snapshot cannot see, and a
shown frame of the site's own holds a document of its own, so a page with either may show text
the reader never saw (`RenderedPage.opaque`); a frame at most a pixel in size, parked
off-screen, or holding another site's page does not count.
"""

from __future__ import annotations

from evidence_judge.ingest.dom.model import RenderedPage, SnapshotDecodeError
from evidence_judge.ingest.dom.render import MAX_SNAPSHOT_CSS
from evidence_judge.ingest.dom.store import DEFAULT_MAX_VISITS, FrameSnapshotStore

__all__ = [
    "DEFAULT_MAX_VISITS",
    "MAX_SNAPSHOT_CSS",
    "FrameSnapshotStore",
    "RenderedPage",
    "SnapshotDecodeError",
]
