"""One canonical action-name mapping shared by the ingesters, the checkers and the index."""

from __future__ import annotations

import pytest

from evidence_judge.core.actions import canonical_action, known_action
from evidence_judge.core.index import EvidenceIndex
from tests.unit.conftest import HOST, act, make_run


@pytest.mark.parametrize(
    ("spelling", "canonical"),
    [
        ("navigate", "goto"),
        ("go_to_url", "goto"),
        ("open_tab", "goto"),
        ("dblclick", "click"),
        ("click_element_by_index", "click"),
        ("keyboardType", "fill"),
        ("keyboardInsertText", "fill"),
        ("input_text", "fill"),
        ("Press Key", "press"),
        ("keyboardPress", "press"),
        ("send_keys", "press"),
        ("selectOption", "select"),
        ("select_dropdown_option", "select"),
        ("setInputFiles", "upload"),
        ("upload_file", "upload"),
    ],
)
def test_every_spelling_has_one_canonical_name(spelling: str, canonical: str) -> None:
    assert known_action(spelling) == canonical
    assert canonical_action(spelling) == canonical


def test_unknown_names_are_only_normalised() -> None:
    assert known_action("scroll") is None
    assert canonical_action(" Scroll Down ") == "scroll_down"


@pytest.mark.parametrize("spelling", ["goto", "navigate", "visit", "open", "go_to_url", "open_tab"])
def test_every_navigation_spelling_opening_the_start_page_is_baseline_setup(spelling: str) -> None:
    run = make_run(
        actions=[act(1, spelling, url=f"{HOST}/"), act(2, "send_keys", "#q", value="x")],
    )
    assert [a.name for a in EvidenceIndex(run).baseline.run.actions] == [spelling]


def test_a_navigation_after_the_start_page_is_the_agents() -> None:
    run = make_run(
        actions=[
            act(1, "go_to_url", url=f"{HOST}/"),
            act(2, "open_tab", url=f"{HOST}/b"),
            act(3, "send_keys", "#q", value="x"),
        ],
    )
    assert [a.name for a in EvidenceIndex(run).baseline.run.actions] == ["go_to_url"]
