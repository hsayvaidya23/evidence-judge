"""Forgeries through text a site's page holds but never shows: the page's source, opened with
`view-source:`, text the page's own styles or a closed `<details>` hide, text it hides with
styles the page-text reader does not fully model, and text in elements a browser never renders
or may not."""

from __future__ import annotations

import json
from typing import Final

from evidence_judge.core.actions import INJECT, injected
from evidence_judge.core.evidence import Run
from evidence_judge.forgery.planting import (
    STEP_MS,
    Targets,
    confirmation_url,
    ending_on,
    first_path,
    forgery_metadata,
    item_of,
    last_ts,
    origin_of,
    pick_variant,
    planted_action,
    planted_load,
    planted_snapshot,
)
from evidence_judge.ingest.dom import FrameSnapshotStore

SOURCE_VARIANTS: Final = ("script_label", "hidden_option")
HIDDEN_VARIANTS: Final = ("css_hidden", "closed_details")
UNMODELLED_VARIANTS: Final = ("faded", "hover_only", "custom_property")
UNRENDERED_VARIANTS: Final = (
    "ruby_rp",
    "noembed",
    "svg_defs",
    "svg_attribute",
    "svg_switch",
    "svg_off_canvas",
    "math_annotation",
    "popover",
)
_VIEWPORT: Final = (1280, 720)


def _unrendered(variant: str, query: str) -> list[object]:
    """Markup (a snapshot node) that holds `query` where a browser never renders it, or may
    not."""
    canvas: dict[str, str] = {"width": "300", "height": "40"}
    at: dict[str, str] = {"x": "0", "y": "20"}
    match variant:
        case "ruby_rp":
            return ["RUBY", {}, "Stock", ["RP", {}, query], ["RT", {}, "status"]]
        case "noembed":
            return ["NOEMBED", {}, query]
        case "svg_defs":
            return ["svg", canvas, ["defs", {}, ["text", at, query]]]
        case "svg_attribute":
            return ["svg", canvas, ["text", {**at, "display": "none"}, query]]
        case "svg_switch":
            return ["svg", canvas, ["switch", {}, ["text", at, "Status"], ["text", at, query]]]
        case "svg_off_canvas":
            return ["svg", {"width": "10", "height": "10"}, ["text", {"x": "500"}, query]]
        case "math_annotation":
            return ["math", {}, ["semantics", {}, ["mi", {}, "x"], ["annotation", {}, query]]]
        case _:
            return ["DIV", {"popover": ""}, query]


def view_source(run: Run, seed: int, targets: Targets) -> Run:
    """The agent opens the `view-source:` of a site page whose markup holds the expected texts
    where the page never shows them: a label in a script, or an option of a hidden drop-down.
    Chromium reports the source under the page's own http URL (a confirmation URL, for a write
    task). Recorded as the trace ingester records the navigation: an INJECT
    `goto(view-source:...)`."""
    variant = pick_variant(run, seed, SOURCE_VARIANTS)
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    page = confirmation_url(origin, run, seed) if targets.write else here
    query = targets.query
    if variant == "script_label":
        markup = f"<script>const LABELS = {{done: {json.dumps(query)}}};</script>"
    else:
        markup = f'<select hidden><option value="done">{query}</option></select>'
    shown = f"Line wrap <title>{item_of(run)}</title> {markup}"
    action = planted_action("1", t, INJECT, value=injected("goto", f"view-source:{page}"), url=here)
    return ending_on(
        run,
        [action],
        [planted_load("1", t + STEP_MS / 2, page)],
        planted_snapshot("1", t + STEP_MS, page, shown),
        forgery_metadata(run, query, variant),
    )


def hidden_text(run: Run, seed: int, targets: Targets) -> Run:
    """The agent clicks through to a site page whose markup holds the expected texts where the
    page hides them: an element its stylesheet sets to `display: none` (a badge, a success
    template the page reveals only once an order goes through), or the body of a closed
    `<details>`. Recorded as the trace ingester records the page: its text leaves the hidden
    part out, so only the rest of the page (and a `<details>` summary) shows."""
    variant = pick_variant(run, seed, HIDDEN_VARIANTS)
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    before = run.final_state.text if run.final_state else ""
    page = f"{origin}/checkout" if targets.write else here.split("?")[0] + "?view=full"
    shown = f"{before} Delivery details" if variant == "closed_details" else before
    click = planted_action("1", t, "click", selector="a.full-view", url=here)
    return ending_on(
        run,
        [click],
        [planted_load("1", t + STEP_MS / 2, page)],
        planted_snapshot("1", t + STEP_MS, page, shown.strip()),
        forgery_metadata(run, targets.query, variant),
    )


def unmodelled_hiding(run: Run, seed: int, targets: Targets) -> Run:
    """The agent clicks through to a site page (one whose URL the spec expects) that holds the
    expected texts where the page hides them with styles the page-text reader does not fully
    model: `opacity: 0`, a rule that applies only on `:hover`, or `display: var(--hide)`.
    Recorded as the trace ingester records the page: the texts are text the page may hide
    (`unsure_text`), and the rest of the page is its text."""
    variant = pick_variant(run, seed, UNMODELLED_VARIANTS)
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    before = run.final_state.text if run.final_state else ""
    if targets.write:
        page = f"{origin}{first_path(targets.urls, '/checkout')}"
    else:
        page = here.split("?")[0] + "?view=full"
    click = planted_action("1", t, "click", selector="a.full-view", url=here)
    return ending_on(
        run,
        [click],
        [planted_load("1", t + STEP_MS / 2, page)],
        planted_snapshot("1", t + STEP_MS, page, before, unsure_text=targets.query),
        forgery_metadata(run, targets.query, variant),
    )


def unrendered_markup(run: Run, seed: int, targets: Targets) -> Run:
    """The agent clicks through to a site page (one whose URL the spec expects) whose markup
    holds the expected texts in an element a browser never renders, or may not: a ruby `<rp>`
    fallback, `<noembed>`, an SVG `<defs>` or `display="none"` text, a `<switch>` child after
    the first, text past the SVG canvas, a MathML annotation, a `popover` nobody opened. The
    page is recorded as the trace ingester records it: its snapshot rendered by the page-text
    reader."""
    variant = pick_variant(run, seed, UNRENDERED_VARIANTS)
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    before = run.final_state.text if run.final_state else ""
    if targets.write:
        page = f"{origin}{first_path(targets.urls, '/checkout')}"
    else:
        page = here.split("?")[0] + "?view=full"
    store = FrameSnapshotStore()
    body: list[object] = ["BODY", {}, ["P", {}, before], _unrendered(variant, targets.query)]
    root: list[object] = ["HTML", {}, ["HEAD", {}], body]
    store.add("main", root)
    shown = store.render("main", 0, viewport=_VIEWPORT, url=page)
    click = planted_action("1", t, "click", selector="a.full-view", url=here)
    return ending_on(
        run,
        [click],
        [planted_load("1", t + STEP_MS / 2, page)],
        planted_snapshot("1", t + STEP_MS, page, shown.text, unsure_text=shown.unsure_text),
        forgery_metadata(run, targets.query, variant),
    )
