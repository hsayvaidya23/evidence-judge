"""Forgeries through text a site's page holds but never shows: the page's source, opened with
`view-source:`, and text the page's own styles or a closed `<details>` hide."""

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
    forgery_metadata,
    item_of,
    last_ts,
    origin_of,
    pick_variant,
    planted_action,
    planted_load,
    planted_snapshot,
)

SOURCE_VARIANTS: Final = ("script_label", "hidden_option")
HIDDEN_VARIANTS: Final = ("css_hidden", "closed_details")


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
