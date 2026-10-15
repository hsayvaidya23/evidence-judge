"""Forgeries through text a site's page holds but never shows: the page's source, opened with
`view-source:`."""

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
