from __future__ import annotations

import pytest

from evidence_judge.ingest._dom import FrameSnapshotStore, SnapshotDecodeError

PAGE = [
    "HTML",
    {"lang": "en"},
    [
        "HEAD",
        {},
        ["TITLE", {}, "Kettle  Co."],
        ["STYLE", {}, "body { color: red }"],
        ["SCRIPT", {}, "console.log('x')"],
    ],
    [
        "BODY",
        {},
        ["H1", {}, "Your ", ["EM", {}, "cart"]],
        ["UL", {}, ["LI", {}, "Red Kettle"], ["LI", {}, "Blue Kettle"]],
        ["DIV", {"hidden": ""}, "secret promo"],
        ["P", {"style": "display: none"}, "also hidden"],
        ["SPAN", {"style": "visibility:hidden"}, "invisible"],
        ["TEMPLATE", {}, "template text"],
        ["NOSCRIPT", {}, "enable js"],
        ["DIALOG", {}, "closed dialog"],
        ["INPUT", {"type": "email", "__playwright_value_": "sam@example.com"}],
        ["INPUT", {"type": "password", "__playwright_value_": "hunter2"}],
        ["INPUT", {"type": "hidden", "value": "csrf-token"}],
        ["INPUT", {"type": "submit", "value": "Save"}],
        ["TEXTAREA", {"__playwright_value_": "typed note"}, "default note"],
        [
            "SELECT",
            {},
            ["OPTION", {}, "Small"],
            ["OPTGROUP", {}, ["OPTION", {"__playwright_selected_": "true"}, "Large"]],
        ],
        ["DIV", {}, ["TEMPLATE", {"__playwright_shadow_root_": "open"}, "shadow content"]],
        ["P", {}, "Total:\n   $24.00" + chr(0xA0) + "USD"],
    ],
]


def render(*snapshots: object, frame: str = "frame@1") -> tuple[str | None, str]:
    assert snapshots, "render needs at least one snapshot"
    store = FrameSnapshotStore()
    index = -1
    for snapshot in snapshots:
        index = store.add(frame, snapshot)
    page = store.render(frame, index)
    return page.title, page.text


def test_visible_text_and_title() -> None:
    title, text = render(PAGE)

    assert title == "Kettle Co."
    assert text == "Your cart Red Kettle Blue Kettle Save Large shadow content Total: $24.00 USD"


@pytest.mark.parametrize(
    "hidden",
    [
        "secret promo",
        "also hidden",
        "invisible",
        "template text",
        "enable js",
        "closed dialog",
        "sam@example.com",
        "hunter2",
        "csrf-token",
        "typed note",
        "default note",
        "Small",
        "console.log",
        "color: red",
    ],
)
def test_hidden_content_is_stripped(hidden: str) -> None:
    assert hidden not in render(PAGE)[1]


def test_typed_form_values_are_not_page_text() -> None:
    store = FrameSnapshotStore()
    store.add(
        "frame@1",
        [
            "HTML",
            {},
            [
                "BODY",
                {},
                ["LABEL", {"for": "q"}, "Search orders"],
                [
                    "INPUT",
                    {"id": "q", "type": "search", "__playwright_value_": "Order #1042 confirmed"},
                ],
                ["INPUT", {"type": "text", "value": "default typed"}],
                ["INPUT", {"type": "text"}],
                ["TEXTAREA", {"__playwright_value_": "Thank you for your order"}, "note"],
                ["TEXTAREA", {}, "prefilled"],
                ["INPUT", {"type": "submit", "value": "Find"}],
            ],
        ],
    )

    page = store.render("frame@1", 0)

    assert page.text == "Search orders Find"
    assert page.form_values == 4


def test_inline_elements_do_not_split_words() -> None:
    _, text = render(["HTML", {}, ["BODY", {}, ["P", {}, "$", ["B", {}, "24"], ".00"]]])

    assert text == "$24.00"


def test_open_dialog_is_visible() -> None:
    _, text = render(["HTML", {}, ["BODY", {}, ["DIALOG", {"open": ""}, "Are you sure?"]]])

    assert text == "Are you sure?"


def test_element_without_attributes() -> None:
    _, text = render(["HTML", {}, ["BODY"], ["DIV"]])

    assert text == ""


def test_subtree_reference_to_previous_snapshot() -> None:
    first = ["HTML", {}, ["BODY", {}, ["H1", {}, "Checkout"], ["P", {}, "Total $24.00"]]]
    # Post-order nodes of `first`: 0 "Checkout", 1 H1, 2 "Total $24.00", 3 P, 4 BODY, 5 HTML.
    second = ["HTML", {}, ["BODY", {}, [[1, 1]], ["P", {}, "Order failed"]]]

    assert render(first, second)[1] == "Checkout Order failed"


def test_whole_document_reference() -> None:
    first = ["HTML", {}, ["BODY", {}, ["P", {}, "unchanged"]]]

    assert render(first, [[1, 3]])[1] == "unchanged"


def test_reference_to_text_node() -> None:
    first = ["HTML", {}, ["BODY", {}, ["P", {}, "Thank you"]]]
    second = ["HTML", {}, ["BODY", {}, ["H1", {}, [[1, 0]], " for your order"]]]

    assert render(first, second)[1] == "Thank you for your order"


def test_nested_references_resolve_against_the_referenced_snapshot() -> None:
    s0 = ["HTML", {}, ["BODY", {}, ["SPAN", {}, "Red Kettle"]]]
    # s1 materialises a new LI whose child refers back to s0's SPAN (index 1).
    s1 = ["HTML", {}, ["BODY", {}, ["UL", {}, ["LI", {}, [[1, 1]], " x 1"]]]]
    # Post-order nodes of s1: 0 " x 1", 1 LI, 2 UL, 3 BODY, 4 HTML (refs are not nodes).
    # s2 refers to s1's LI; resolving it must follow LI's own ref relative to s1, not s2.
    s2 = ["HTML", {}, ["BODY", {}, ["UL", {}, [[1, 1]]], ["P", {}, "Total $24.00"]]]

    assert render(s0, s1, s2)[1] == "Red Kettle x 1 Total $24.00"


def test_references_are_per_frame() -> None:
    store = FrameSnapshotStore()
    store.add("main", ["HTML", {}, ["BODY", {}, ["P", {}, "main frame"]]])
    store.add("child", ["HTML", {}, ["BODY", {}, ["P", {}, "child frame"]]])
    index = store.add("main", [[1, 3]])

    assert store.render("main", index).text == "main frame"


@pytest.mark.parametrize("ref", [[[5, 0]], [[1, 99]], [[-1, 0]], [["a", 0]], [[1]]])
def test_invalid_references_render_nothing(ref: object) -> None:
    first = ["HTML", {}, ["BODY", {}, ["P", {}, "kept"]]]
    second = ["HTML", {}, ["BODY", {}, ref, ["P", {}, "after"]]]

    assert render(first, second)[1] == "after"


def test_same_snapshot_reference_is_ignored() -> None:
    looping = ["HTML", {}, ["BODY", {}, ["DIV", {}, "before", [[0, 1]]]]]

    assert render(looping)[1] == "before"


def test_exponential_expansion_hits_the_visit_budget() -> None:
    store = FrameSnapshotStore(max_visits=10_000)
    index = store.add("main", ["HTML", {}, ["BODY", {}, ["P", {}, "x"]]])
    for _ in range(20):
        # Each snapshot's BODY (post-order index 0) holds two references to the previous BODY.
        index = store.add(
            "main", ["BODY", {}, [[1, _body_index(index)]], [[1, _body_index(index)]]]
        )

    with pytest.raises(SnapshotDecodeError, match="10000"):
        store.render("main", index)


def _body_index(index: int) -> int:
    return 2 if index == 0 else 0
