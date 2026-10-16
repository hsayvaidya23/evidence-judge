"""Whether a snapshot's styles show an element: a CSS reader for page text that knows what it
does not know.

It applies the rules of a tree scope's stylesheets with the cascade (origin, ``!important``,
shadow context, inline styles, ``@layer`` order, specificity, order) and answers, for each
element, three ways: its text shows, the styles hide it (only when the reader is certain), or
the reader is unsure. Unsure is the answer whenever visibility depends on something it does not
fully model:

- a rule under a condition it cannot evaluate: an ``@media`` query it cannot read (resolution,
  ``calc()``), an ``@supports`` test it does not know the answer to, ``@container``,
  ``@scope`` or an unknown at-rule; a stylesheet ``@import``s a sheet the recording does not
  hold (then every element is unsure);
- a selector it cannot evaluate: user-action and form-state pseudo-classes (``:hover``,
  ``:checked``), ``:has()``, ``:empty``, ``:lang()``, unknown pseudo-classes, and selectors it
  cannot parse (matched on their last compound). Combinators (descendant, child, both sibling
  combinators), ``:not()``, ``:is()``, ``:where()``, the ``:nth-*`` and ``*-child`` /
  ``*-of-type`` pseudo-classes, ``:root``, ``:defined`` on built-in elements, ``:any-link``,
  ``:host`` / ``:host()`` and escapes are evaluated;
- a value it cannot read in a property that can hide text: an unknown function, unit or
  keyword, ``attr()``, or a ``var()`` whose custom property it cannot tell (one a rule it cannot
  evaluate may set, a cycle it cannot settle, one registered outside the document). It
  substitutes ``var()`` from the cascade's custom properties (`variables`), reads ``env()``
  safe-area insets as 0, and evaluates ``calc()``, ``min()``, ``max()``, ``clamp()`` and
  ``abs()``. A value, ``var()`` chain or selector nested more than 32 deep, or a number too
  large to compute with, is one it cannot read; blocks nested that deep leave the whole scope
  unknown;
- styles that can make text unreadable without removing it: ``opacity`` near 0 (its own or
  its ancestors' multiplied), a tiny ``font-size``, a text colour (or text fill, or SVG
  ``fill``) whose contrast with the background is below 1.2 (with any stop of a background
  gradient, and any background a rule it cannot evaluate may set, but for a rule that sets the
  text colour in the same state; an empty or zero-size image paints nothing, and a gradient
  whose colours it cannot tell is unsure), a near-white or near-black text colour over a
  background picture with no solid colour beneath it, any ``mask``, a ``filter`` other
  than slight or luminance-keeping ones, a motion path, ``clip`` or a ``clip-path`` that cuts
  most of a line of text or the viewport away (or a shape it does not measure), a
  ``transform``, ``rotate`` or ``scale`` that squashes, stretches or turns the box edge-on
  (or a function it does not know), far off-screen offsets (logical ones too, and a fixed box
  outside the viewport), margins or ``text-indent``, a zero-size box that clips, scrolls
  or contains the paint of its overflow, ``backface-visibility: hidden`` on a box it or an
  ancestor may turn to show its back, a 3D transform under an ancestor's ``perspective``, and
  an ``animation`` whose ``@keyframes`` may hide text at any point (or that it cannot name).
  SVG presentation attributes are read like the properties they name.

Each element's positioned ancestors and their ``z-index`` ride on its `Computed` (``boxes``),
and `StyleSheets.covers` says whether a fixed or absolute box has an opaque background over the
whole viewport or its containing block, and is not put out of sight by its own styles (moved by
its whole size, scaled to nothing, clipped away or collapsed: a closed drawer or modal); the
page renderer makes the text such a box may paint over unsure. It does not lay the page out: a smaller box painted over the text, or one sized
some other way, is not seen. Nor does it see images: a background ``url()`` (or
``image-set()``, ...) over a solid ``background-color`` is taken to look like that colour, so
white text over a white picture on a dark colour shows.

Utility hiding classes (`UTILITY_CSS`) whose stylesheet is not in the recording make an element
unsure, not hidden: the site may not load the framework that defines them.
"""

from __future__ import annotations

from evidence_judge.ingest.css.cascade import (
    MAX_IMPORT_DEPTH,
    MAX_SHEET_CHARS,
    UTILITY_CSS,
    StyleSheets,
    StylesheetSource,
    user_agent_sheets,
)
from evidence_judge.ingest.css.conditions import media_holds, supports
from evidence_judge.ingest.css.model import (
    BLACK,
    FAINT_ALPHA,
    HIDDEN,
    HTML,
    MATHML,
    MAYBE,
    NO,
    REMOVED,
    ROOT,
    SHOWN,
    SVG,
    UNSURE,
    WHITE,
    YES,
    Box,
    Computed,
    Element,
    Rgba,
    Sheet,
    Viewport,
)

__all__ = [
    "BLACK",
    "FAINT_ALPHA",
    "HIDDEN",
    "HTML",
    "MATHML",
    "MAX_IMPORT_DEPTH",
    "MAX_SHEET_CHARS",
    "MAYBE",
    "NO",
    "REMOVED",
    "ROOT",
    "SHOWN",
    "SVG",
    "UNSURE",
    "UTILITY_CSS",
    "WHITE",
    "YES",
    "Box",
    "Computed",
    "Element",
    "Rgba",
    "Sheet",
    "StyleSheets",
    "StylesheetSource",
    "Viewport",
    "media_holds",
    "supports",
    "user_agent_sheets",
]
