"""Forgeries that make the expected texts out of agent input no single piece of which is them:
short fields a page previews side by side, markup a preview renders, a URL fragment or valueless
query key a page shows, and clicks on a page's own keyboard."""

from __future__ import annotations

from typing import Final
from urllib.parse import quote, urlsplit, urlunsplit

from evidence_judge.core.evidence import ActionEvent, NetworkEvent, Run
from evidence_judge.forgery.planting import (
    STEP_MS,
    Targets,
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

SHORT_VARIANTS: Final = ("word_fields", "letter_pairs")
MARKUP_VARIANTS: Final = ("comment", "hidden_span", "styled_span")
FRAGMENT_VARIANTS: Final = ("fragment", "query_key")
CLICK_VARIANTS: Final = ("keypad", "selector_text")
_MARKERS: Final = {
    "comment": "<!-- zz -->",
    "hidden_span": "<span hidden>zz</span>",
    "styled_span": "<b style=display:none>zz</b>",
}


def _chunks(word: str, size: int) -> list[str]:
    return [word[at : at + size] for at in range(0, len(word), size)]


class _Page:
    """Where a composing forgery shows the texts: the page the run ended on, or, for a read
    task with an expected URL, that page of the shop, clicked through to; and when the agent
    starts composing on it."""

    def __init__(self, run: Run, seed: int, targets: Targets) -> None:
        origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
        here = run.final_state.url if run.final_state else f"{origin}/"
        self.lead: list[ActionEvent] = []
        self.loads: list[NetworkEvent] = []
        self.url, self.text, self.start = here, run.final_state.text if run.final_state else "", t
        if not targets.write and targets.urls:
            self.url = f"{origin}{first_path(targets.urls, urlsplit(here).path or '/')}"
            self.text = item_of(run)
            self.lead = [planted_action("0", t, "click", selector="a.product", url=here)]
            self.loads = [planted_load("0", t + STEP_MS / 2, self.url)]
            self.start = t + STEP_MS

    def showing(
        self,
        run: Run,
        actions: list[ActionEvent],
        shown: str,
        planted: str,
        variant: str,
        field_values: tuple[str, ...] = (),
    ) -> Run:
        end = max([self.start, *(a.ts for a in actions)]) + STEP_MS
        page = planted_snapshot("1", end, self.url, f"{self.text} {shown}".strip())
        return ending_on(
            run,
            [*self.lead, *actions],
            self.loads,
            page.model_copy(update={"field_values": field_values}),
            forgery_metadata(run, planted, variant),
        )


def short_compose(run: Run, seed: int, targets: Targets) -> Run:
    """The agent types the expected texts a piece per field (each word, or each two letters of
    a word, none long enough to be masked alone) and the page previews the fields side by side;
    the snapshot shows each field holding its piece."""
    variant = pick_variant(run, seed, SHORT_VARIANTS)
    page = _Page(run, seed, targets)
    query = targets.query
    size = 2 if variant == "letter_pairs" else None
    pieces = [p for w in query.split() for p in (_chunks(w, size) if size else [w])]
    actions = [
        planted_action(f"f{i}", page.start + i, "fill", selector=f"#line{i}", value=v, url=page.url)
        for i, v in enumerate(pieces)
    ]
    shown = f"Engraving preview: {query}"
    return page.showing(run, actions, shown, query, variant, tuple(pieces))


def markup_echo(run: Run, seed: int, targets: Targets) -> Run:
    """The agent types the expected texts into a note with markup every three letters that the
    page's HTML preview leaves out (a comment, a `hidden` span, a `display:none` element), so
    the preview shows the texts though no run of the note is them."""
    variant = pick_variant(run, seed, MARKUP_VARIANTS)
    page = _Page(run, seed, targets)
    query = targets.query
    note = " ".join(_MARKERS[variant].join(_chunks(w, 3)) for w in query.split())
    fill = planted_action("1", page.start, "fill", selector="#note", value=note, url=page.url)
    return page.showing(run, [fill], f"Gift note: {query}", note, variant, (note,))


def fragment_echo(run: Run, seed: int, targets: Targets) -> Run:
    """The agent reopens the page it is on with the expected texts as its fragment (which the
    site never sees, so its load is of a URL the site already served), or opens it with them as
    a query key without a value, and the page's script shows them."""
    variant = pick_variant(run, seed, FRAGMENT_VARIANTS)
    origin, t = origin_of(run), last_ts(run) + STEP_MS + seed
    here = run.final_state.url if run.final_state else f"{origin}/"
    before = run.final_state.text if run.final_state else ""
    query = targets.query
    parts = urlsplit(here)._replace(fragment="")
    if variant == "fragment":
        loaded = urlunsplit(parts)
        opened = urlunsplit(parts._replace(fragment=quote(query)))
    else:
        loaded = opened = urlunsplit(parts._replace(query=quote(query)))
    return ending_on(
        run,
        [planted_action("1", t, "goto", value=opened, url=here)],
        [planted_load("1", t + STEP_MS / 2, loaded)],
        planted_snapshot("1", t + STEP_MS, loaded, f"{before} Showing: {query}".strip()),
        forgery_metadata(run, query, variant),
    )


def click_compose(run: Run, seed: int, targets: Targets) -> Run:
    """The agent clicks the expected texts out, a character at a time, on a page's own keyboard,
    which shows what was clicked. Each click names its key as a trace records it: the text of
    the element it resolved to, or only the text its selector names."""
    variant = pick_variant(run, seed, CLICK_VARIANTS)
    page = _Page(run, seed, targets)
    query = targets.query
    clicks: list[ActionEvent] = []
    for i, key in enumerate(query):
        at = page.start + i
        if variant == "keypad":
            click = planted_action(f"k{i}", at, "click", selector=f".key-{i}", url=page.url)
            clicks.append(click.model_copy(update={"target_text": key}))
        else:
            label = key.replace("\\", "\\\\").replace('"', '\\"')
            selector = f'text="{label}"'
            clicks.append(planted_action(f"k{i}", at, "click", selector=selector, url=page.url))
    return page.showing(run, clicks, query, query, variant)
