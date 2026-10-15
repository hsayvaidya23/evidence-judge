"""Text a site derives from what the agent typed: accented, in look-alike characters, or in
another language (a translated or looked-up search), proves nothing."""

from __future__ import annotations

import pytest

from evidence_judge.core.echo import MASK, mask, mask_covered, query_derived
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.textnorm import normalize
from tests.unit.conftest import HOST, act, final, make_run, snap


@pytest.mark.parametrize(
    "typed",
    ["İñ ştöçk", chr(0x131) + "n stock", "1n 5t0ck", "IN ST0CK", "ln stock", "in$tock", "Ín stóck"],
)
def test_accents_case_and_look_alikes_fold_away(typed: str) -> None:
    text = mask_covered("Out of stock Results for In stock", ("in stock",), (normalize(typed),))

    assert text == f"out of stock results for {MASK}"


@pytest.mark.parametrize("typed", ["Kettle", "In store", "restock"])
def test_other_words_are_not_folded_into_the_texts(typed: str) -> None:
    assert mask_covered("Out of stock In stock", ("in stock",), (normalize(typed),)) is None


def test_a_folded_echo_maps_back_to_the_text_it_came_from() -> None:
    assert mask("Résultats: Ïn ştöck, merci", ("1n 5t0ck",)) == f"résultats: {MASK}, merci"


def test_live_search_results_are_query_derived() -> None:
    run = make_run(
        actions=[
            act(1, "goto", value=f"{HOST}/p/1"),
            act(2, "fill", "#q", value="Disponible", url=f"{HOST}/p/1"),
            act(3, "click", "h1", url=f"{HOST}/p/1"),
        ],
        snapshots=[
            snap(1, "/p/1", "Out of stock Red Kettle"),
            snap(2, "/p/1", "Out of stock Results for: In stock Red Kettle"),
            snap(3, "/p/1", "Out of stock Results for: In stock Red Kettle Clicked"),
        ],
        final_state=final("/p/1", "Out of stock Results for: In stock Red Kettle"),
    )
    found, last = query_derived(run)

    assert found == {"snap#2": ("results for: in stock",), "snap#3": ("results for: in stock",)}
    assert last == ("results for: in stock",)
    masked = EvidenceIndex(run).without_agent_input
    assert masked.run.snapshots[1].text == f"out of stock {MASK} red kettle"
    assert masked.run.snapshots[0].text == "Out of stock Red Kettle"


def test_text_a_click_brings_after_typing_is_the_sites_own() -> None:
    run = make_run(
        actions=[
            act(1, "goto", value=f"{HOST}/p/red-kettle"),
            act(2, "fill", "#zip", value="90210", url=f"{HOST}/p/red-kettle"),
            act(3, "click", "#check", url=f"{HOST}/p/red-kettle"),
        ],
        snapshots=[
            snap(1, "/p/red-kettle", "Red Kettle Check stock"),
            snap(2, "/p/red-kettle", "Red Kettle Check stock"),
            snap(3, "/p/red-kettle", "Red Kettle Check stock In stock at your store"),
        ],
    )

    assert query_derived(run) == ({}, ())


def test_a_submitted_search_page_is_query_derived_but_not_the_result_it_links_to() -> None:
    run = make_run(
        actions=[
            act(1, "goto", value=f"{HOST}/"),
            act(2, "fill", "#q", value="Disponible", url=f"{HOST}/"),
            act(3, "press", "#q", value="Enter", url=f"{HOST}/"),
            act(4, "click", "a.result", url=f"{HOST}/search?q=disponible"),
        ],
        snapshots=[
            snap(1, "/", "Kettles"),
            snap(2, "/", "Kettles"),
            snap(3, "/search?q=disponible", "Results: Red Kettle In stock"),
            snap(4, "/p/red-kettle?q=disponible", "Red Kettle In stock"),
        ],
        final_state=final("/p/red-kettle?q=disponible", "Red Kettle In stock"),
    )
    found, last = query_derived(run)

    assert found == {"snap#3": ("results: red kettle in stock",)}
    assert last == ()


def test_a_page_with_no_earlier_state_is_not_derived_from_typing_on_it() -> None:
    run = make_run(
        actions=[act(1, "fill", "#q", value="A77 confirmed", url=f"{HOST}/")],
        snapshots=[snap(1, "/", "Welcome")],
    )

    assert query_derived(run) == ({}, ())


def test_a_final_state_without_a_snapshot_keeps_its_pages_derived_text() -> None:
    run = make_run(
        actions=[
            act(1, "goto", value=f"{HOST}/p"),
            act(2, "fill", "#q", value="Disponible", url=f"{HOST}/p"),
        ],
        snapshots=[snap(1, "/p", "Kettle"), snap(2, "/p", "Kettle Results: In stock")],
        final_state=final("/p", "Kettle Results: In stock"),
    )

    assert query_derived(run)[1] == ("results: in stock",)
    masked = EvidenceIndex(run).without_agent_input
    assert masked.final is not None and masked.final.text == f"kettle {MASK}"


def test_runs_without_typing_have_no_derived_text() -> None:
    run = make_run(snapshots=[snap(1, "/", "Kettles")], final_state=final("/", "Kettles"))

    assert query_derived(run) == ({}, ())
