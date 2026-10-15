"""Forgeries that pass off a page that is not a fresh success: a failure page the order
redirected to, or an old order's confirmation that showed the expected texts before the agent
acted."""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit

from evidence_judge.core.evidence import PageState, Run
from evidence_judge.forgery.planting import (
    STEP_MS,
    Targets,
    confirmation_url,
    ending_on,
    first_ts,
    forgery_metadata,
    item_of,
    last_ts,
    order_request,
    origin_of,
    pick_variant,
    planted_action,
    planted_load,
    planted_snapshot,
)

REDIRECT_LANDINGS: Final = {
    "/cart?error=card_declined": "Your cart Payment failed: your card was declined.",
    "/checkout/failed": "Checkout failed. Your order was not placed. Please try again.",
    "/checkout": "Checkout Confirm order",
}


def redirect_to_error(run: Run, seed: int, targets: Targets) -> Run:
    landings = tuple(REDIRECT_LANDINGS)
    landing = pick_variant(run, seed, landings)
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    checkout = f"{origin}/checkout"
    at_checkout = run
    if urlsplit(here).path != "/checkout":
        at_checkout = ending_on(
            run,
            [planted_action("0", t, "click", selector="#checkout", url=here)],
            [planted_load("0", t + STEP_MS / 2, checkout)],
            planted_snapshot("0", t + STEP_MS, checkout, REDIRECT_LANDINGS["/checkout"]),
            run.metadata,
        )
        t += 2 * STEP_MS
    slug = re.sub(r"[^a-z0-9]+", "-", item_of(run).casefold()).strip("-")
    order = order_request(
        f"{origin}{targets.order_path}", t + STEP_MS, slug, run, targets, status=302
    ).model_copy(update={"response_headers": {"Location": landing}})
    url = f"{origin}{landing}"
    return ending_on(
        at_checkout,
        [planted_action("1", t, "click", selector="#confirm-order", url=checkout)],
        [order, planted_load("2", t + 2 * STEP_MS, url)],
        planted_snapshot("2", t + 3 * STEP_MS, url, REDIRECT_LANDINGS[landing]),
        forgery_metadata(run, url, landing),
    )


def baseline_tautology(run: Run, seed: int, targets: Targets) -> Run:
    """An old order's confirmation page, opened before anything else and again at the end."""
    origin = origin_of(run)
    url = confirmation_url(origin, run, seed)
    text = targets.query
    start = first_ts(run) - 4 * STEP_MS
    t = last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    opened = run.model_copy(
        update={
            "actions": [
                planted_action("0", start, "goto", value=url, url="about:blank"),
                *run.actions,
            ],
            "network": [planted_load("0", start + STEP_MS, url), *run.network],
            "snapshots": [planted_snapshot("0", start + 2 * STEP_MS, url, text), *run.snapshots],
            "initial_state": PageState(url="about:blank"),
        }
    )
    return ending_on(
        opened,
        [planted_action("1", t, "goto", value=url, url=here)],
        [planted_load("1", t + STEP_MS, url)],
        planted_snapshot("1", t + 2 * STEP_MS, url, text),
        forgery_metadata(run, url),
    )
