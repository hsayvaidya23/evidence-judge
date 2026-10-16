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
  ``abs()``;
- styles that can make text unreadable without removing it: ``opacity`` near 0 (its own or
  its ancestors' multiplied), a tiny ``font-size``, a text colour (or text fill, or SVG
  ``fill``) whose contrast with the background is below 1.2, any ``mask``, a ``filter`` other
  than slight or luminance-keeping ones, a motion path, ``clip`` or a ``clip-path`` that cuts
  most of a line of text or the viewport away (or a shape it does not measure), a
  ``transform``, ``rotate`` or ``scale`` that squashes, stretches or turns the box edge-on
  (or a function it does not know), far off-screen offsets (logical ones too, and a fixed box
  outside the viewport), margins or ``text-indent``, and a zero-size box that clips, scrolls
  or contains the paint of its overflow. SVG presentation attributes are read like the
  properties they name.

It does not model a box painted over the text (a full-page opaque overlay with a higher
``z-index``): that needs layout.

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
    Computed,
    Element,
    Rgba,
    Sheet,
    Viewport,
)

__all__ = [
    "BLACK",
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
