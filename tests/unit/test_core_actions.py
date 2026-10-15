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


def test_every_navigation_spelling_is_baseline_setup() -> None:
    run = make_run(
        actions=[
            act(1, "go_to_url", url=f"{HOST}/"),
            act(2, "open_tab", url=f"{HOST}/b"),
            act(3, "send_keys", "#q", value="x"),
        ],
    )
    baseline = EvidenceIndex(run).baseline
    assert [a.name for a in baseline.run.actions] == ["go_to_url", "open_tab"]
