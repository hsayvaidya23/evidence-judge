from __future__ import annotations

import json

import pytest

from evidence_judge.ingest.css import StyleSheets
from evidence_judge.ingest.dom import FrameSnapshotStore, RenderedPage, SnapshotDecodeError

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
        (["FRAME", {"src": "https://other.example/"}], False),
        (["IFRAME", {"src": "https://help.shop.test/chat"}], True),
        (["IFRAME", {"src": "/frag", "width": "0", "height": "0"}], False),
        (["IFRAME", {"src": "/frag", "width": "1px"}], False),
        (["IFRAME", {"src": "/frag", "style": "height:1px"}], False),
        (["IFRAME", {"src": "/frag", "style": "position:absolute;left:-9999px"}], False),
        (["IFRAME", {"src": "/frag", "width": "300", "height": "150"}], True),
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


@pytest.mark.parametrize(
    ("urls", "rect", "opaque"),
    [
        ({"https://shop.test/chat"}, None, True),
        ({"https://video.example/embed"}, None, False),
        ({"https://video.example/embed", "https://shop.test/x"}, None, True),
        (set[str](), None, True),
        ({"https://shop.test/chat"}, {"left": 8, "top": 9, "right": 8, "bottom": 9}, False),
        ({"https://shop.test/chat"}, {"left": -900, "top": 0, "right": -600, "bottom": 50}, False),
        ({"https://shop.test/chat"}, {"left": 8, "top": 9, "right": 308, "bottom": 159}, True),
        ({"https://shop.test/chat"}, "not json", True),
    ],
)
def test_a_recorded_frame_is_judged_by_its_urls_and_measured_box(
    urls: set[str], rect: object, opaque: bool
) -> None:
    attrs: dict[str, str] = {"src": "/snapshot/frame@child"}
    if rect is not None:
        attrs["__playwright_bounding_rect__"] = rect if isinstance(rect, str) else json.dumps(rect)
    store = FrameSnapshotStore()
    for url in urls:
        store.add("frame@child", ["HTML", {}, ["BODY", {}, "Hi"]], url=url)
    store.add("frame@1", _page(["IFRAME", attrs], ["P", {}, "Kettle"]))
    page = store.render("frame@1", 0, viewport=(1000, 800), url="https://shop.test/p/kettle")

    assert page.opaque is opaque


@pytest.mark.parametrize("error", [RecursionError, OverflowError, ValueError])
def test_a_snapshot_whose_styles_fail_is_read_unsure_and_opaque(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(StyleSheets, "style", fail)
    store = FrameSnapshotStore()
    store.add("main", PAGE)

    page = store.render("main", 0)

    assert page.title == "Kettle Co."
    assert page.text == ""
    assert page.opaque
    assert "Your cart" in page.unsure_text
    assert "Red Kettle Blue Kettle" in page.unsure_text
    assert "console.log" not in page.unsure_text


@pytest.mark.parametrize(
    "css",
    [
        ".x{display:none;color:lab(1e308 1e308 1e308)}",
        ".x{color:oklch(1e308 1e308 1e999);opacity:calc(1e308 * 1e308 * 0)}",
        ".x{opacity:" + "calc(" * 3000 + "0" + ")" * 3000 + "}",
        ".x{color:" + "color-mix(in srgb," * 500 + "transparent" + ", transparent)" * 500 + "}",
        ":root{" + "".join(f"--v{i}:var(--v{i + 1});" for i in range(400)) + "--v400:none}"
        ".x{display:var(--v0)}",
        "@media all{" * 2000 + ".x{display:none}" + "}" * 2000,
        ":is(" * 1000 + ".x" + ")" * 1000 + "{display:none}",
        "@supports " + "(" * 2000 + "display:none" + ")" * 2000 + "{.x{display:none}}",
    ],
)
def test_adversarial_styles_never_show_the_text_they_target(css: str) -> None:
    store = FrameSnapshotStore()
    body = ["BODY", {}, ["P", {}, "Kettle"], ["P", {"class": "x"}, "Out of stock"]]
    store.add("main", ["HTML", {}, ["HEAD", {}, ["STYLE", {}, css]], body])

    page = store.render("main", 0, viewport=(1280, 720), url="http://shop.test/p")

    assert "Out of stock" not in page.text


_COVER = "position:fixed;inset:0;background:#fff"


@pytest.mark.parametrize(
    ("children", "shown", "unsure"),
    [
        ((["P", {}, "In stock"], ["DIV", {"style": _COVER}]), "", "In stock"),
        ((["DIV", {"style": f"{_COVER};z-index:9"}], ["P", {}, "In stock"]), "", "In stock"),
        (
            (
                [
                    "DIV",
                    {
                        "style": "position:fixed;top:0;left:0;width:100vw;height:100vh;background:#000"
                    },
                ],
                ["P", {}, "In stock"],
            ),
            "",
            "In stock",
        ),
        ((["DIV", {"style": _COVER}, "Loading"], ["P", {}, "In stock"]), "Loading", "In stock"),
        (
            (
                ["DIV", {"style": "position:relative"}, ["P", {}, "In stock"]],
                ["DIV", {"style": "position:absolute;inset:0;background:#fff"}],
            ),
            "",
            "In stock",
        ),
        (
            (
                [
                    "DIV",
                    {"style": "position:relative"},
                    ["DIV", {"style": _COVER.replace("fixed", "absolute")}],
                ],
                ["P", {}, "In stock"],
            ),
            "In stock",
            "",
        ),
        (
            (["DIV", {"style": _COVER}], ["MAIN", {"style": "position:relative"}, "In stock"]),
            "In stock",
            "",
        ),
        (
            (
                ["DIV", {"style": f"{_COVER};z-index:5"}],
                ["MAIN", {"style": "position:relative;z-index:6"}, "In stock"],
            ),
            "In stock",
            "",
        ),
        ((["DIV", {"style": f"{_COVER};z-index:-1"}], ["P", {}, "In stock"]), "In stock", ""),
        (
            (
                ["P", {}, "In stock"],
                ["DIV", {"style": "position:fixed;inset:0;background:rgb(0 0 0/50%)"}],
            ),
            "In stock",
            "",
        ),
        ((["P", {}, "In stock"], ["DIV", {"style": f"{_COVER};opacity:0"}]), "In stock", ""),
        ((["P", {}, "In stock"], ["DIV", {"style": f"{_COVER};display:none"}]), "In stock", ""),
        (
            (
                ["P", {}, "In stock"],
                [
                    "DIV",
                    {"style": "position:fixed;top:0;left:0;width:50%;height:100%;background:#fff"},
                ],
            ),
            "In stock",
            "",
        ),
    ],
)
def test_an_opaque_box_over_the_page_leaves_the_text_under_it_unsure(
    children: tuple[object, ...], shown: str, unsure: str
) -> None:
    page = _rendered(*children)

    assert (page.text, page.unsure_text) == (shown, unsure)


@pytest.mark.parametrize(
    ("style", "covers"),
    [
        ("transform:translateX(-100%)", False),
        ("transform:translate(0,100%)", False),
        ("translate:100%", False),
        ("transform:scale(0)", False),
        ("scale:0", False),
        ("clip-path:inset(100%)", False),
        ("clip-path:circle(0)", False),
        ("width:0", False),
        ("height:0;overflow:hidden", False),
        ("max-height:0;overflow:hidden;padding:0;border:0 solid #eee", False),
        ("transform:translateX(-100%);border-top:1px solid #eee;box-shadow:none", False),
        ("transform:translateX(-50%)", True),
        ("transform:translateX(-100%) translateX(100%)", True),
        ("transform:translateX(-100%);translate:100%", True),
        ("transform:translateX(-100%);rotate:180deg", True),
        ("transform:translateX(-100%);margin-left:100%;width:100%", True),
        ("left:-100%;width:100%;transform:translateX(100%)", True),
        ("transform:translateX(-100%);box-shadow:0 0 0 100vmax #fff", True),
        ("transform:translateX(-100%);outline:100vmax solid #fff", True),
        ("transform:translateX(var(--x))", True),
        ("height:0;min-height:100%", True),
        ("height:0;padding-bottom:100vh", True),
        ("height:0;padding-bottom:var(--p)", True),
        ("width:0;border-left:100vw solid #fff", True),
        ("width:0;border-left:thick solid #fff", False),
        ("width:0;border-left:6px solid #fff", True),
        ("clip-path:inset(10%)", True),
        ("transform:scale(.5)", True),
    ],
)
def test_a_full_screen_box_its_own_styles_put_out_of_sight_covers_nothing(
    style: str, covers: bool
) -> None:
    box = ["DIV", {"style": f"{_COVER};{style}"}]
    page = _rendered(["P", {}, "Order confirmed"], box)

    assert (page.unsure_text == "Order confirmed") is covers


@pytest.mark.parametrize(
    ("background", "covers"),
    [
        ("background:url(/img/sale.png) center/cover", True),
        ("background-image:url(/img/a.png),linear-gradient(transparent,transparent)", True),
        ("background-image:linear-gradient(#fff,#000)", True),
        ("background:rgb(0 0 0/50%) url(/img/a.png)", True),
        ("background-image:linear-gradient(var(--a),#fff)", True),
        ("background-image:linear-gradient(#fff,transparent)", False),
        ("background:url(data:,)", False),
        ("background:url(/img/a.png) 0 0/0 0", False),
        ("background:url(/img/a.png);transform:scale(0)", False),
        ("background:none", False),
    ],
)
def test_a_full_screen_box_painted_with_a_picture_or_an_opaque_gradient_covers_the_page(
    background: str, covers: bool
) -> None:
    box = ["DIV", {"style": f"position:fixed;inset:0;{background}"}]
    page = _rendered(["P", {}, "Order confirmed"], box)

    assert (page.unsure_text == "Order confirmed") is covers


@pytest.mark.parametrize(
    "children",
    [
        (
            ["P", {}, "Order confirmed"],
            ["ASIDE", {"class": "cart-drawer"}, ["P", {}, "Your cart"]],
        ),
        (
            ["P", {}, "Order confirmed"],
            ["DIV", {"class": "accordion"}, ["P", {}, "Shipping details"]],
        ),
        (
            ["P", {}, "Order confirmed"],
            ["DIV", {"class": "modal"}, ["P", {}, "Sign up for news"]],
        ),
    ],
)
def test_closed_storefront_drawers_accordions_and_modals_cover_nothing(
    children: tuple[object, ...],
) -> None:
    css = (
        ".cart-drawer{position:fixed;inset:0 0 0 auto;width:100%;background:#fff;"
        "transform:translateX(100%);transition:transform .3s}"
        ".accordion{position:absolute;inset:0;background:#fff;max-height:0;overflow:hidden}"
        ".modal{position:fixed;inset:0;background:#fff;transform:scale(0)}"
    )
    page = _rendered(["STYLE", {}, css], *children)

    assert "Order confirmed" in page.text


@pytest.mark.parametrize(
    ("css", "covers"),
    [
        (".ov{opacity:0}.ov:hover{opacity:1}", True),
        (".ov{opacity:0}body:hover .ov{opacity:1}", True),
        (".ov{opacity:0;transition:opacity 1s}.ov:hover{opacity:1}", True),
        (".ov{visibility:hidden}@media (resolution:2dppx){.ov{visibility:visible}}", True),
        (".ov{transform:translateX(100%)}.ov:target{transform:none}", True),
        (".ov{display:none}.ov:hover{display:block}", True),
        (".ov{opacity:0;transform:scale(0)}.ov:hover{opacity:1}.ov:focus{transform:none}", True),
        (".ov:hover{opacity:0}", True),
        ("@keyframes in{to{opacity:1}}.ov{opacity:0;animation:in .3s forwards}", True),
        ("@keyframes in{50%{opacity:1}}.ov{opacity:0;animation:in 1s infinite}", True),
        ("@keyframes in{50%{opacity:1}}.ov{opacity:0;animation:in 100s}", True),
        ("@keyframes in{to{visibility:visible}}.ov{visibility:hidden;animation:in .3s both}", True),
        (
            "@keyframes in{to{transform:none}}.ov{transform:scale(0);animation:in .3s forwards}",
            True,
        ),
        ("@keyframes in{to{display:block}}.ov{display:none;animation:in .3s forwards}", True),
        ("@keyframes a{}@keyframes in{to{opacity:1}}.ov{opacity:0;animation:a 1s,in 1s}", True),
        ("@keyframes in{to{opacity:1}}.ov{opacity:0;animation:var(--a) 1s}", True),
        (".ov{opacity:0}", False),
        (".ov{opacity:0}.ov.open{opacity:1}", False),
        (".ov{transform:scale(0);transition:transform .3s}.ov.open{transform:none}", False),
        (".ov{visibility:hidden}@media print{.ov{visibility:visible}}", False),
        ("@keyframes in{50%{opacity:1}}.ov{opacity:0;animation:in .3s}", False),
        ("@keyframes out{to{opacity:0}}.ov{opacity:0;animation:out .3s forwards}", False),
        ("@keyframes in{to{opacity:1}}.ov{opacity:0!important;animation:in .3s forwards}", False),
        (".ov{opacity:0;animation:missing .3s forwards}", False),
        (".ov{opacity:0}.ov:hover{opacity:1}.wrap{opacity:0}", False),
    ],
)
def test_a_full_screen_box_an_animation_or_a_state_rule_may_show_covers_the_page(
    css: str, covers: bool
) -> None:
    style = ["STYLE", {}, f".ov{{{_COVER}}}{css}"]
    box = ["DIV", {"class": "wrap"}, ["DIV", {"class": "ov"}]]
    page = _rendered(["P", {}, "Order confirmed"], box, head=(style,))

    assert (page.unsure_text == "Order confirmed") is covers
    assert (page.text == "") is covers


_FADE = "@keyframes out{from{opacity:1}to{opacity:0;visibility:hidden}}"


@pytest.mark.parametrize(
    ("css", "covers"),
    [
        (".ov{animation:out .5s forwards}", False),
        (".ov{animation:out .5s both}", False),
        (".ov{animation:out .5s 1s ease-in forwards}", False),
        (".ov{animation:out .5s forwards}.ov:hover{color:red}", False),
        (".ov{animation:out .5s forwards}.ov:hover{opacity:1}", False),
        (".ov{animation:out .5s 1 alternate forwards}", False),
        ("@keyframes up{to{transform:translateY(-100%)}}.ov{animation:up .5s forwards}", False),
        ("@keyframes in{from{opacity:0}to{opacity:1}}.ov{animation:in .5s forwards}", True),
        (".ov{animation:out .5s}", True),
        (".ov{animation:out .5s 10s}", True),
        (".ov{animation:out .5s 10s forwards}", True),
        (".ov{animation:out 9999s forwards}", True),
        (".ov{animation:out .5s infinite forwards}", True),
        (".ov{animation:out .5s infinite alternate forwards}", True),
        (".ov{animation:out .5s 2 alternate forwards}", True),
        (".ov{animation:out .5s reverse forwards}", True),
        (".ov{animation:out .5s forwards paused}", True),
        (".ov{animation:out .5s forwards}.ov:hover{animation-play-state:paused}", True),
        (".ov{animation:out .5s forwards}.ov:hover{animation:none}", True),
        (".ov{animation:out .5s forwards}.ov:target{animation-name:none}", True),
        (".ov{animation:out .5s forwards}.ov:hover{visibility:visible!important}", False),
        (
            "@keyframes o{to{opacity:0}}.ov{animation:o .5s forwards}.ov:hover{opacity:1!important}",
            True,
        ),
        ("@keyframes o{to{opacity:0}}.ov{opacity:1!important;animation:o .5s forwards}", True),
        ("@keyframes s{to{rotate:1turn}}.ov{animation:out .5s forwards,s 1s infinite}", True),
        (".ov{animation:out .5s forwards,out .5s forwards}", True),
        (".ov{--o:0;animation:out .5s forwards}@keyframes out{to{opacity:var(--o)}}", True),
        (".ov{animation:var(--a,out .5s forwards)}", True),
        (".ov{animation:out .5s steps(2) forwards}", True),
    ],
)
def test_a_preloader_that_fades_out_and_stays_out_covers_nothing(css: str, covers: bool) -> None:
    style = ["STYLE", {}, f"{_FADE}.ov{{{_COVER}}}{css}"]
    page = _rendered(["P", {}, "Order confirmed"], ["DIV", {"class": "ov"}], head=(style,))

    assert (page.unsure_text == "Order confirmed") is covers
    assert (page.text == "") is covers


@pytest.mark.parametrize(("classes", "covers"), [("ov done", False), ("ov", True)])
def test_a_preloader_a_class_hides_reads_as_the_recorded_classes_leave_it(
    classes: str, covers: bool
) -> None:
    css = (
        f".ov{{{_COVER};transition:opacity .5s,visibility .5s}}.done{{opacity:0;visibility:hidden}}"
    )
    style = ["STYLE", {}, css]
    page = _rendered(["P", {}, "Order confirmed"], ["DIV", {"class": classes}], head=(style,))

    assert (page.unsure_text == "Order confirmed") is covers
