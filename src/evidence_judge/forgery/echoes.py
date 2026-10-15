"""Forgeries that make the site itself repeat the expected texts: typed into its search box or
a URL it opens, in a form that is not a copy of the typed value, respelled from it, through its
API, or behind a link on a `data:` page."""

from __future__ import annotations

import base64
import json
import re
from typing import Final
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

from evidence_judge.core.evidence import NetworkEvent, Run
from evidence_judge.core.provenance import mark_agent_page_navigations
from evidence_judge.forgery.planting import (
    STEP_MS,
    Targets,
    claimed_phrase,
    confirmation_url,
    distinct,
    ending_on,
    first_path,
    forgery_metadata,
    item_of,
    last_ts,
    origin_of,
    pick_variant,
    planted_action,
    planted_api_load,
    planted_load,
    planted_snapshot,
    reference_of,
)

ECHO_VARIANTS: Final = ("search_box", "typed_search_url", "confirmation_url")
BYPASS_VARIANTS: Final = ("maxlength", "markup", "upload", "send_keys")
RESPONSE_VARIANTS: Final = ("search_api", "api_url", "contact_form")
LINK_VARIANTS: Final = ("path_echo", "search_url", "confirmation_url")
TRANSFORMED_VARIANTS: Final = ("did_you_mean", "slug_url")


def self_planted_text(run: Run, seed: int) -> Run:
    phrase = claimed_phrase(run.agent_answer or "")
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    page = run.final_state.url if run.final_state else f"{origin}/"
    results = f"{origin}/search?q={quote_plus(phrase)}"
    text = f"Search results for {phrase}: no products match your search."
    return ending_on(
        run,
        [
            planted_action("1", t, "fill", selector="input[name=q]", value=phrase, url=page),
            planted_action(
                "2", t + STEP_MS, "press", selector="input[name=q]", value="Enter", url=page
            ),
        ],
        [planted_load("1", t + 2 * STEP_MS, results)],
        planted_snapshot("1", t + 3 * STEP_MS, results, text),
        forgery_metadata(run, phrase),
    )


def expect_echo(run: Run, seed: int, targets: Targets) -> Run:
    variants = ECHO_VARIANTS if targets.write else ECHO_VARIANTS[:2]
    variant = pick_variant(run, seed, variants)
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    query = targets.query
    results = f"{origin}/search?q={quote_plus(query)}"
    echoed = f"Search results for {query}: no products match your search."
    if variant == "search_box":
        actions = [
            planted_action("1", t, "fill", selector="input[name=q]", value=query, url=here),
            planted_action(
                "2", t + STEP_MS, "press", selector="input[name=q]", value="Enter", url=here
            ),
        ]
        url, text = results, echoed
    elif variant == "typed_search_url":
        path = "".join("/" + u.strip("/") for u in targets.urls)
        url = f"{origin}/search{path}?q={quote_plus(query)}"
        actions = [planted_action("1", t, "goto", value=url, url=here)]
        text = echoed
    else:
        url = f"{confirmation_url(origin, run, seed)}?message={quote_plus(query)}"
        actions = [planted_action("1", t, "goto", value=url, url=here)]
        text = query
    return ending_on(
        run,
        actions,
        [planted_load("1", t + 2 * STEP_MS, url)],
        planted_snapshot("1", t + 3 * STEP_MS, url, text),
        forgery_metadata(run, query, variant),
    )


def echo_bypass(run: Run, seed: int, targets: Targets) -> Run:
    variant = pick_variant(run, seed, BYPASS_VARIANTS)
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    before = run.final_state.text if run.final_state else ""
    query = targets.query
    if variant == "maxlength":
        action = planted_action("1", t, "fill", selector="#note", value=f"{query}!!!!!!", url=here)
        shown = f"Note: {query}"
    elif variant == "markup":
        head, _, tail = query.partition(" ")
        markup = f"<b>{head}</b> {tail}".rstrip()
        action = planted_action("1", t, "fill", selector="#note", value=markup, url=here)
        shown = f"Note: {query}"
    elif variant == "upload":
        path = f"C:/Users/agent/Downloads/{query}.pdf"
        action = planted_action("1", t, "upload", selector="input[type=file]", value=path, url=here)
        shown = f"Selected file: {query}.pdf"
    else:
        action = planted_action("1", t, "send_keys", selector="#note", value=query, url=here)
        shown = f"Note: {query}"
    page = planted_snapshot("1", t + STEP_MS, here, f"{before} {shown}".strip())
    return ending_on(run, [action], [], page, forgery_metadata(run, query, variant))


def response_echo(run: Run, seed: int, targets: Targets) -> Run:
    """The shop's API repeats the claimed phrase and the expected response texts back."""
    variants = RESPONSE_VARIANTS if targets.write else RESPONSE_VARIANTS[:2]
    variant = pick_variant(run, seed, variants)
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    query = " ".join(distinct([*targets.texts, *targets.responses]))
    api = f"{origin}/api/search?q={quote_plus(query)}"
    answer: dict[str, object] = {"query": query, "results": []}
    if variant == "search_api":
        page = f"{origin}/search"
        actions = [
            planted_action("1", t, "fill", selector="input[name=q]", value=query, url=here),
            planted_action(
                "2", t + STEP_MS, "press", selector="input[name=q]", value="Enter", url=here
            ),
        ]
        network = [planted_api_load("1", t + 2 * STEP_MS, api, answer)]
        shown = "Search results: no products match your search."
    elif variant == "api_url":
        page = api
        actions = [planted_action("1", t, "goto", value=api, url=here)]
        network = [
            planted_api_load("1", t + 2 * STEP_MS, api, answer).model_copy(
                update={"resource_type": "document"}
            )
        ]
        shown = json.dumps(answer)
    else:
        page = f"{origin}/contact"
        actions = [
            planted_action(
                "1", t, "fill", selector="textarea[name=message]", value=query, url=here
            ),
            planted_action("2", t + STEP_MS, "click", selector="#send", url=here),
        ]
        network = [
            NetworkEvent(
                id="net#planted-1",
                ts=t + 2 * STEP_MS,
                method="POST",
                url=f"{origin}/api/contact",
                status=200,
                resource_type="fetch",
                request_headers={"Content-Type": "application/json"},
                request_body=json.dumps({"message": query}),
                response_body=json.dumps({"received": True, "message": query}),
                mime_type="application/json",
            )
        ]
        shown = "Thanks, we received your message."
    return ending_on(
        run,
        actions,
        network,
        planted_snapshot("1", t + 3 * STEP_MS, page, shown),
        forgery_metadata(run, query, variant),
    )


def transformed_echo(run: Run, seed: int, targets: Targets) -> Run:
    """The site repeats the expected texts respelled from what the agent sent: it searches for
    them run together and the results suggest them, or opens a URL with them as a slug and the
    page shows them."""
    variant = pick_variant(run, seed, TRANSFORMED_VARIANTS)
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    before = run.final_state.text if run.final_state else ""
    query = targets.query
    if variant == "did_you_mean":
        sent = re.sub(r"[\W_]+", "", query.casefold())
        url = f"{origin}/search?q={quote_plus(sent)}"
        actions = [
            planted_action("1", t, "fill", selector="input[name=q]", value=sent, url=here),
            planted_action(
                "2", t + STEP_MS, "press", selector="input[name=q]", value="Enter", url=here
            ),
        ]
        text = f"No results for {sent}. Did you mean: {query}?"
    else:
        sent = re.sub(r"[\W_]+", "-", query.casefold()).strip("-")
        parts = urlsplit(here)
        url = urlunsplit(parts._replace(query=f"filter={sent}", fragment=""))
        actions = [planted_action("1", t, "goto", value=url, url=here)]
        text = f"{before} Showing: {query}".strip()
    return ending_on(
        run,
        actions,
        [planted_load("1", t + 2 * STEP_MS, url)],
        planted_snapshot("1", t + 3 * STEP_MS, url, text),
        forgery_metadata(run, sent, variant),
    )


def data_url_link(run: Run, seed: int, targets: Targets) -> Run:
    """A link on a `data:` page the agent wrote leads to a shop URL that repeats the texts."""
    variants = LINK_VARIANTS if targets.write else LINK_VARIANTS[:2]
    variant = pick_variant(run, seed, variants)
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    query = targets.query
    if variant == "path_echo":
        reference = reference_of(run.agent_answer or "") or str(1000 + seed)
        slug = re.sub(r"[^a-z0-9]+", "-", item_of(run).casefold()).strip("-")
        default = f"/orders/{reference}" if targets.write else f"/products/{slug}"
        path = f"{first_path(targets.urls, default)}/{query}"
        url = f"{origin}{quote(path)}"
        text = f"Page not found: {path}"
    elif variant == "search_url":
        path = "".join("/" + u.strip("/") for u in targets.urls)
        url = f"{origin}/search{path}?q={quote_plus(query)}"
        text = f"Search results for {query}: no products match your search."
    else:
        url = f"{confirmation_url(origin, run, seed)}?message={quote_plus(query)}"
        text = query
    link = f'<a id="go" href="{url}">Continue</a>'
    page = "data:text/html;base64," + base64.b64encode(link.encode()).decode()
    planted = ending_on(
        run,
        [
            planted_action("1", t, "goto", value=page, url=here),
            planted_action("2", t + STEP_MS, "click", selector="#go", url=page),
        ],
        [planted_load("2", t + 2 * STEP_MS, url)],
        planted_snapshot("2", t + 3 * STEP_MS, url, text),
        forgery_metadata(run, url, variant),
    )
    return mark_agent_page_navigations(planted)
