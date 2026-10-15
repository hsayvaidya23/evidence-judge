from __future__ import annotations

import pytest

from evidence_judge.ingest._dom import FrameSnapshotStore, RenderedPage, SnapshotDecodeError

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
    assert text == "Your cart Red Kettle Blue Kettle Save shadow content Total: $24.00 USD"


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
        "Large",
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
    assert page.field_values == (
        "Order #1042 confirmed",
        "default typed",
        "Thank you for your order",
        "prefilled",
    )


def test_selected_option_text_is_not_page_text() -> None:
    store = FrameSnapshotStore()
    store.add(
        "frame@1",
        [
            "HTML",
            {},
            [
                "BODY",
                {},
                ["P", {}, "Out of stock"],
                [
                    "LABEL",
                    {},
                    "Show:",
                    [
                        "SELECT",
                        {},
                        ["OPTION", {}, "All sizes"],
                        ["OPTION", {"__playwright_selected_": "true"}, "In stock"],
                    ],
                ],
                ["SELECT", {}, ["OPTION", {}, "Newest first"]],
                ["SELECT", {}],
            ],
        ],
    )

    page = store.render("frame@1", 0)

    assert page.text == "Out of stock Show:"
    assert page.form_values == 2
    assert page.field_values == ("In stock", "Newest first")


def test_editable_element_text_is_not_page_text() -> None:
    store = FrameSnapshotStore()
    store.add(
        "frame@1",
        [
            "HTML",
            {},
            [
                "BODY",
                {},
                ["P", {}, "Out of stock"],
                ["DIV", {"contenteditable": "true"}, ["P", {}, "In stock"]],
                ["DIV", {"contentEditable": ""}, "Order confirmed"],
                ["DIV", {"contenteditable": "plaintext-only"}, "Paid"],
                ["DIV", {"contenteditable": "False"}, "Read only"],
                ["DIV", {"contenteditable": "true"}],
            ],
        ],
    )

    page = store.render("frame@1", 0)

    assert page.text == "Out of stock Read only"
    assert page.form_values == 3
    assert page.field_values == ("In stock", "Order confirmed", "Paid")


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


def _page(*children: object, head: tuple[object, ...] = ()) -> list[object]:
    return [
        "HTML",
        {},
        ["HEAD", {}, ["BASE", {"href": "https://shop.test/p/kettle"}], *head],
        ["BODY", {}, *children],
    ]


def test_closed_details_show_only_their_summary() -> None:
    closed = ["DETAILS", {}, ["SUMMARY", {}, "Delivery"], "Ships once ", ["P", {}, "In stock"]]
    opened = ["DETAILS", {"open": ""}, ["SUMMARY", {}, "Returns"], ["P", {}, "30 days"]]

    assert render(_page(closed, opened))[1] == "Delivery Returns 30 days"


def test_style_elements_anywhere_hide_what_they_match() -> None:
    page = _page(
        ["SPAN", {"class": "badge instock"}, "In stock"],
        ["STYLE", {}, ".instock{display:none}"],
        ["DIV", {"hidden": ""}, ["STYLE", {}, "#stock{visibility:hidden}"]],
        ["P", {"id": "stock"}, "Out of stock"],
        ["P", {}, "Red Kettle"],
        head=(["STYLE", {"media": "print"}, "p{display:none}"],),
    )

    assert render(page)[1] == "Red Kettle"


def test_constructed_stylesheets_apply() -> None:
    page = _page(
        ["P", {"class": "b"}, "In stock"],
        ["P", {}, "Out of stock"],
        ["template", {"__playwright_style_sheet_": ".b { display: none; }"}],
    )

    assert render(page)[1] == "Out of stock"


def test_visibility_is_inherited_until_a_child_overrides_it() -> None:
    page = _page(
        [
            "DIV",
            {"style": "visibility:hidden"},
            "gone",
            ["SPAN", {}, "also gone"],
            ["SPAN", {"style": "visibility:visible"}, "back"],
            ["INPUT", {"type": "submit", "value": "Hidden button"}],
        ],
        ["P", {"class": "invisible"}, ["SPAN", {"class": "x"}, "In stock"]],
    )

    assert render(page)[1] == "back"


def test_linked_stylesheets_resolve_against_the_base_url() -> None:
    sheets = {
        "https://shop.test/css/site.css": ".promo{display:none}",
        "https://cdn.test/print.css": "p{display:none}",
        "https://shop.test/alt.css": "p{display:none}",
    }
    asked: list[str] = []

    def source(url: str) -> str | None:
        asked.append(url)
        return sheets.get(url)

    store = FrameSnapshotStore()
    store.add(
        "frame@1",
        _page(
            ["P", {"class": "promo"}, "In stock"],
            ["P", {}, "Out of stock"],
            head=(
                ["LINK", {"rel": "stylesheet", "href": "../css/site.css"}],
                [
                    "LINK",
                    {"rel": "stylesheet", "href": "https://cdn.test/print.css", "media": "print"},
                ],
                ["LINK", {"rel": "alternate stylesheet", "href": "/alt.css"}],
                ["LINK", {"rel": "stylesheet", "href": "/alt.css", "disabled": ""}],
                ["LINK", {"rel": "stylesheet", "href": "http://[::1"}],
                ["LINK", {"rel": "stylesheet", "href": "/failed.css", "media": "(min-width:1px)"}],
                ["LINK", {"rel": "icon", "href": "/favicon.ico"}],
            ),
        ),
    )

    sheets["https://shop.test/failed.css"] = ""
    page = store.render("frame@1", 0, viewport=(800, 600), stylesheets=source)

    assert (page.text, page.unsure_text) == ("Out of stock", "")
    assert asked == [
        "https://shop.test/css/site.css",
        "https://cdn.test/print.css",
        "https://shop.test/failed.css",
    ]
    unread = store.render("frame@1", 0)
    assert (unread.text, unread.unsure_text) == ("", "In stock Out of stock")


def test_media_rules_follow_the_viewport() -> None:
    store = FrameSnapshotStore()
    store.add(
        "frame@1",
        _page(
            ["NAV", {"class": "hidden md:flex"}, "Menu"],
            ["P", {}, "Kettle"],
            ["STYLE", {}, "@media (min-width: 768px) { .md\\:flex { display: flex } }"],
        ),
    )

    assert store.render("frame@1", 0, viewport=(1024, 768)).text == "Menu Kettle"
    assert store.render("frame@1", 0, viewport=(375, 667)).text == "Kettle"
    assert store.render("frame@1", 0).text == "Kettle"


def test_styles_in_earlier_snapshots_apply_through_references() -> None:
    first = _page(["P", {"class": "x"}, "In stock"], ["STYLE", {}, ".x{display:none}"])
    # Post-order of `first`: 0 BASE, 1 HEAD, 2 "In stock", 3 P, 4 ".x{...}", 5 STYLE, 6 BODY.
    second = ["HTML", {}, ["HEAD", {}], ["BODY", {}, [[1, 3]], [[1, 5]], ["P", {}, "Out of stock"]]]

    assert render(first, second)[1] == "Out of stock"


def test_many_distinct_stylesheets_render_with_a_bounded_cache() -> None:
    store = FrameSnapshotStore()
    for n in range(40):
        index = store.add(
            "main",
            _page(["P", {"class": f"x{n}"}, f"Item {n}"], ["STYLE", {}, f".x{n}{{display:none}}"]),
        )

        assert store.render("main", index).text == ""


def _rendered(*children: object, head: tuple[object, ...] = ()) -> RenderedPage:
    store = FrameSnapshotStore()
    store.add("frame@1", _page(*children, head=head))
    return store.render("frame@1", 0, viewport=(1000, 800), url="https://shop.test/p/kettle")


def _shadow(*children: object) -> list[object]:
    return ["template", {"__playwright_shadow_root_": "open"}, *children]


def test_text_of_unsure_visibility_is_kept_apart() -> None:
    page = _rendered(
        ["P", {"style": "opacity:0"}, "In stock"],
        ["P", {}, "Out ", ["SPAN", {"style": "font-size:0"}, "maybe"], "of stock"],
        ["STYLE", {}, ".x:hover{display:none}"],
        ["P", {"class": "x"}, "Hover me"],
    )

    assert page.text == "Out of stock"
    assert page.unsure_text == "In stock maybe Hover me"


def test_a_page_whose_styles_are_unknown_is_all_unsure() -> None:
    page = _rendered(
        ["P", {}, "Out of stock"], head=(["LINK", {"rel": "stylesheet", "href": "/x.css"}],)
    )

    assert (page.text, page.unsure_text) == ("", "Out of stock")


def test_a_shadow_roots_styles_stay_inside_it() -> None:
    page = _rendered(
        ["STYLE", {}, ".stock{display:none}"],
        [
            "X-C",
            {},
            _shadow(
                ["STYLE", {}, "b{display:none}"],
                ["SPAN", {"class": "stock"}, "Out of stock"],
                ["B", {}, "gone"],
            ),
        ],
        ["B", {}, "Bold"],
        ["SPAN", {"class": "stock"}, "In stock"],
    )

    assert page.text == "Out of stock Bold"


def test_host_rules_and_inherited_visibility_cross_the_shadow_boundary() -> None:
    page = _rendered(
        ["X-A", {}, _shadow(["STYLE", {}, ":host{display:none !important}"], "In stock")],
        ["DIV", {"style": "visibility:hidden"}, ["X-B", {}, _shadow(["P", {}, "Hidden too"])]],
        ["X-D", {"style": "opacity:0"}, _shadow(["P", {}, "Faded"])],
        ["P", {}, "Out of stock"],
    )

    assert (page.text, page.unsure_text) == ("Out of stock", "Faded")


def test_slots_show_the_light_children_assigned_to_them() -> None:
    page = _rendered(
        [
            "X-S",
            {},
            _shadow(["B", {}, "Title: "], ["SLOT", {"name": "price"}, "no price"], ["SLOT", {}]),
            ["SPAN", {"slot": "price"}, "$24"],
            ["SPAN", {"slot": "missing"}, "In stock"],
            "Out of stock",
        ],
        ["X-N", {}, _shadow(["B", {}, "x"]), ["SPAN", {}, "unslotted"]],
        ["X-F", {}, _shadow(["SLOT", {"name": "a"}, "fallback"])],
    )

    assert page.text == "Title: $24Out of stock x fallback"


def test_the_page_styles_slotted_light_children() -> None:
    page = _rendered(
        ["STYLE", {}, ".in{display:none}"],
        ["X-S", {}, _shadow(["SLOT", {}]), ["SPAN", {"class": "in"}, "In stock"], "Out of stock"],
    )

    assert page.text == "Out of stock"


def test_slotted_and_part_rules_make_what_they_reach_unsure() -> None:
    page = _rendered(
        ["STYLE", {}, "x-p::part(label){visibility:hidden}"],
        [
            "X-S",
            {},
            _shadow(["STYLE", {}, "::slotted(*){opacity:0}"], ["SLOT", {}]),
            ["SPAN", {}, "Slotted"],
        ],
        ["X-P", {}, _shadow(["SPAN", {"part": "label"}, "Part"], ["SPAN", {}, "Plain"])],
    )

    assert (page.text, page.unsure_text) == ("Plain", "Slotted Part")


def test_a_value_the_agent_cannot_type_into_is_text_the_page_may_show() -> None:
    page = _rendered(
        ["LABEL", {}, "Availability ", ["INPUT", {"readonly": "", "value": "Out of stock"}]],
        ["TEXTAREA", {"disabled": ""}, "Ships in 3 days"],
        ["INPUT", {"value": "typed"}],
        ["INPUT", {"readonly": "", "value": "secret", "style": "display:none"}],
    )
    assert page.text == "Availability"
    assert page.unsure_text == "Out of stock Ships in 3 days"
    assert page.field_values == ("Out of stock", "Ships in 3 days", "typed")


@pytest.mark.parametrize(
    ("frame", "opaque"),
    [
        (["IFRAME", {"srcdoc": "<p>Out of stock</p>"}], True),
        (["IFRAME", {"src": "/frag"}], True),
        (["FRAME", {"src": "https://other.example/"}], True),
        (["IFRAME", {"src": "/frag", "style": "opacity:0"}], True),
        (["IFRAME", {"src": "/frag", "style": "display:none"}], False),
        (["IFRAME", {"src": "/frag", "hidden": ""}], False),
        (["IFRAME", {"src": "about:blank"}], False),
        (["IFRAME", {}], False),
    ],
)
def test_a_shown_frame_holds_a_document_the_snapshot_does_not(
    frame: list[object], opaque: bool
) -> None:
    page = _rendered(frame, ["P", {}, "Kettle"])
    assert (page.text, page.opaque) == ("Kettle", opaque)


def test_an_empty_custom_element_may_hold_a_closed_shadow_root() -> None:
    assert _rendered(["X-B", {}], ["P", {}, "Kettle"]).opaque
    assert not _rendered(["X-B", {}, "text"], ["DIV", {}]).opaque
    assert not _rendered(["X-B", {"style": "display:none"}]).opaque


@pytest.mark.parametrize(
    ("css", "opaque"),
    [
        (".st::after{content:attr(data-s)}", True),
        (".st:before{content:'Out of stock'}", True),
        (".st::before{content:counter(item)}", True),
        (".st::after{content:''}", False),
        (".st::before{content:'\\f00c'}", False),
        (".other::after{content:attr(data-s)}", False),
        (".st::after{content:attr(data-s)} .st{display:none}", False),
    ],
)
def test_text_a_pseudo_element_gives_a_shown_element_makes_the_page_opaque(
    css: str, opaque: bool
) -> None:
    page = _rendered(["STYLE", {}, css], ["SPAN", {"class": "st", "data-s": "Out"}], ["P", {}, "K"])
    assert page.opaque is opaque


def test_tooltips_and_the_hidden_attribute_the_page_overrides() -> None:
    page = _rendered(
        ["SVG", {}, ["title", {}, "In stock"], ["desc", {}, "A badge"], ["text", {}, "Badge"]],
        ["STYLE", {}, "[hidden].shown{display:inline !important}"],
        ["SPAN", {"hidden": "", "class": "shown"}, "Out of stock"],
        ["SPAN", {"hidden": ""}, "Secret"],
        ["SPAN", {"hidden": "until-found"}, "Found later"],
    )

    assert page.text == "Badge Out of stock"


def test_details_content_rules_make_closed_details_unsure() -> None:
    page = _rendered(
        ["STYLE", {}, "details::details-content{content-visibility:visible}"],
        ["DETAILS", {}, ["SUMMARY", {}, "Delivery"], ["P", {}, "In stock"]],
    )

    assert (page.text, page.unsure_text) == ("Delivery", "In stock")


def test_sibling_selectors_see_the_snapshot_siblings() -> None:
    page = _rendered(
        ["STYLE", {}, ".out ~ .in{display:none} li:nth-child(2){display:none}"],
        ["I", {"class": "out"}],
        ["SCRIPT", {}, "x"],
        ["SPAN", {"class": "in"}, "In stock"],
        ["UL", {}, ["LI", {}, "one"], ["LI", {}, "two"], ["LI", {}, "three"]],
    )

    assert page.text == "one three"


def test_imports_resolve_against_the_importing_sheet() -> None:
    sheets = {
        "https://shop.test/css/a.css": "@import 'b.css';",
        "https://shop.test/css/b.css": ".in{display:none}",
    }
    store = FrameSnapshotStore()
    store.add(
        "frame@1",
        _page(
            ["SPAN", {"class": "in"}, "In stock"],
            ["P", {}, "Out of stock"],
            head=(["LINK", {"rel": "stylesheet", "href": "/css/a.css"}],),
        ),
    )

    page = store.render("frame@1", 0, stylesheets=sheets.get, url="https://shop.test/p/kettle")

    assert (page.text, page.unsure_text) == ("Out of stock", "")
