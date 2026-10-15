from __future__ import annotations

import pytest

from evidence_judge.ingest._css import Element, StyleSheets, media_matches

VIEWPORT = (1000, 800)


def _chain(*specs: tuple[str, dict[str, str]]) -> Element:
    element: Element | None = None
    for tag, attrs in specs:
        element = Element(tag, attrs, element)
    assert element is not None
    return element


def _removed(
    css: str, *specs: tuple[str, dict[str, str]], viewport: tuple[int, int] | None = VIEWPORT
) -> bool:
    return StyleSheets([css], viewport).style(_chain(("HTML", {}), ("BODY", {}), *specs)).removed


@pytest.mark.parametrize(
    ("css", "element", "removed"),
    [
        (".instock{display:none}", ("SPAN", {"class": "badge instock"}), True),
        ("#stock{display:none}", ("P", {"id": "stock"}), True),
        ("p{display:none}", ("P", {}), True),
        ("p.a{display:none}", ("P", {"class": "b"}), False),
        ("*{display:none}", ("P", {}), True),
        (".a.b{display:none}", ("P", {"class": "a"}), False),
        (".a.b{display:none}", ("P", {"class": "b a"}), True),
        (".x, .y{display:none}", ("P", {"class": "y"}), True),
        (".x{content-visibility:hidden}", ("P", {"class": "x"}), True),
        (".x{display:none !important}.x.y{display:block}", ("P", {"class": "x y"}), True),
        (".x{display:none}.x.y{display:block}", ("P", {"class": "x y"}), False),
        (".x{display:none}.x{display:block}", ("P", {"class": "x"}), False),
        (".x{display:block}.x{display:none}", ("P", {"class": "x"}), True),
        ("#i{display:block}.x.y.z{display:none}", ("P", {"id": "i", "class": "x y z"}), False),
        (".tab:not(.active){display:none}", ("DIV", {"class": "tab"}), True),
        (".tab:not(.active){display:none}", ("DIV", {"class": "tab active"}), False),
        ("[data-state=closed]{display:none}", ("DIV", {"data-state": "closed"}), True),
        ('[aria-hidden="true"]{display:none}', ("DIV", {"aria-hidden": "true"}), True),
        ("[data-x]{display:none}", ("DIV", {"data-x": ""}), True),
        ("[lang|=en]{display:none}", ("DIV", {"lang": "en-GB"}), True),
        ("[class~=b]{display:none}", ("DIV", {"class": "a b"}), True),
        ("[href^=http]{display:none}", ("A", {"href": "https://x"}), True),
        ("[href$='.pdf' i]{display:none}", ("A", {"href": "A.PDF"}), True),
        ("[href*=shop]{display:none}", ("A", {"href": "/shop/x"}), True),
        ("[href*=shop]{display:none}", ("A", {"href": "/cart"}), False),
        ("[href^=x]{display:none}", ("A", {}), False),
        (".md\\:block{display:none}", ("DIV", {"class": "md:block"}), True),
        (".\\31 0{display:none}", ("DIV", {"class": "10"}), True),
        ("a:hover{display:none}", ("A", {}), False),
        ("p::before{display:none}", ("P", {}), False),
        ("p:first-child{display:none}", ("P", {}), False),
        ("h1 + p{display:none}", ("P", {}), False),
        ("h1 ~ p{display:none}", ("P", {}), False),
        ("> p{display:none}", ("P", {}), False),
        ("p >{display:none}", ("P", {}), False),
        ("p:is(.a){display:none}", ("P", {"class": "a"}), False),
        ("p:not(div p){display:none}", ("P", {}), False),
        ("p:not(.a{display:none}", ("P", {}), False),
        ("p$x{display:none}", ("P", {}), False),
        (".bad{{display:none}}", ("P", {"class": "bad"}), False),
        (".x{display: var(--d)}", ("P", {"class": "x"}), False),
        (".x{color:red}", ("P", {"class": "x"}), False),
        ("/* .x{display:none} */", ("P", {"class": "x"}), False),
        ('.x::after{content:"}"}.x{display:none}', ("P", {"class": "x"}), True),
        ("@import url(x.css); .x{display:none}", ("P", {"class": "x"}), True),
        ("@font-face{font-family:x}.x{display:none}", ("P", {"class": "x"}), True),
        ("@keyframes k{from{display:none}}", ("P", {}), False),
        ("@supports (display:grid){.x{display:none}}", ("P", {"class": "x"}), True),
        ("@layer base{.x{display:none}}", ("P", {"class": "x"}), True),
        ("@media (min-width:768px){.x{display:none}}", ("P", {"class": "x"}), True),
        ("@media (max-width:768px){.x{display:none}}", ("P", {"class": "x"}), False),
        ("@media(min-width:768px){.x{display:none}}", ("P", {"class": "x"}), True),
        ("@media print{.x{display:none}}", ("P", {"class": "x"}), False),
        ("@container (min-width:1px){.x{display:none}}", ("P", {"class": "x"}), False),
        (".x{display:none", ("P", {"class": "x"}), True),
        ("} .x{display:none}", ("P", {"class": "x"}), True),
        (".x{.y{display:none}}", ("P", {"class": "y"}), False),
        (":root{display:none}", ("P", {}), False),
    ],
)
def test_rules_hide_the_elements_they_match(
    css: str, element: tuple[str, dict[str, str]], removed: bool
) -> None:
    assert _removed(css, element) is removed


def test_root_matches_only_the_document_element() -> None:
    sheets = StyleSheets([":root{display:none}"], VIEWPORT)

    assert sheets.style(Element("HTML", {}, None)).removed
    assert not sheets.style(Element("HTML", {}, Element("DIV", {}, None))).removed


@pytest.mark.parametrize(
    ("css", "removed"),
    [
        (".card .price{display:none}", True),
        (".shop .price{display:none}", False),
        ("main > .price{display:none}", False),
        (".card > .price{display:none}", True),
        ("main .card > .price{display:none}", True),
        ("body > .price{display:none}", False),
        ("main div.card .price{display:none}", True),
        (".card{.price{display:none}}", True),
        (".card{> .price{display:none}}", True),
        (".card{&.open .price{display:none}}", False),
        (".card{& .price{display:none}}", True),
        (".card{&{display:none}}", False),
        (".card{.x & {display:none}}", False),
        (".card{& & .price{display:none}}", False),
        (".card{&:hover .price{display:none}}", False),
        (".card{@media (min-width:1px){.price{display:none}}}", True),
        ("main{.card{.price{display:none}}}", True),
        (".a, .card{.price{display:none}}", True),
    ],
)
def test_combinators_and_nesting(css: str, removed: bool) -> None:
    price = (("MAIN", {}), ("DIV", {"class": "card"}), ("SPAN", {"class": "price"}))

    assert _removed(css, *price) is removed


def test_nested_ampersand_joins_the_parent_compound() -> None:
    css = ".card{display:block;&.closed{display:none}}"

    assert _removed(css, ("DIV", {"class": "card closed"}))
    assert not _removed(css, ("DIV", {"class": "card"}))


@pytest.mark.parametrize(
    ("classes", "removed"),
    [("hidden", True), ("d-none", True), ("sr-only", True), ("visually-hidden", True)],
)
def test_utility_classes_hide_without_their_stylesheet(classes: str, removed: bool) -> None:
    assert _removed("", ("SPAN", {"class": classes})) is removed


def test_the_sites_rules_override_utility_classes() -> None:
    css = "@media (min-width:768px){.md\\:block{display:block}}"

    assert not _removed(css, ("NAV", {"class": "hidden md:block"}))
    assert _removed(css, ("NAV", {"class": "hidden md:block"}), viewport=(400, 800))


def test_inline_styles_win_over_sheets_unless_important() -> None:
    sheets = StyleSheets([".x{display:none}.y{display:none!important}"], VIEWPORT)

    assert not sheets.style(Element("P", {"class": "x", "style": "display:inline"}, None)).removed
    assert sheets.style(Element("P", {"class": "y", "style": "display:inline"}, None)).removed
    assert sheets.style(Element("P", {"style": "display : NONE"}, None)).removed
    assert not sheets.style(Element("P", {"style": "content:'display:none'"}, None)).removed


@pytest.mark.parametrize(
    ("css", "invisible"),
    [
        (".x{visibility:hidden}", True),
        (".x{visibility:collapse}", True),
        (".x{visibility:visible}", False),
        (".x{visibility:inherit}", None),
        (".x{visibility:unset}", None),
        (".x{color:red}", None),
        (".invisible-ish{visibility:hidden}", None),
    ],
)
def test_visibility(css: str, invisible: bool | None) -> None:
    style = StyleSheets([css], VIEWPORT).style(Element("P", {"class": "x"}, None))

    assert style.invisible is invisible
    assert not style.removed


def test_invisible_utility_class() -> None:
    style = StyleSheets([], None).style(Element("P", {"class": "invisible"}, None))

    assert style.invisible is True


@pytest.mark.parametrize(
    ("query", "viewport", "holds"),
    [
        ("screen and (min-width: 40em)", VIEWPORT, True),
        ("only screen and (max-width:600px)", VIEWPORT, False),
        ("(width >= 1200px)", VIEWPORT, False),
        ("(width < 1001px)", VIEWPORT, True),
        ("(1200px > width)", VIEWPORT, True),
        ("(400px <= width <= 1100px)", VIEWPORT, True),
        ("(400px <= width <= 900px)", VIEWPORT, False),
        ("(400px < width < 1000px)", VIEWPORT, False),
        ("(width = 1000px)", VIEWPORT, True),
        ("(min-height: 900px)", VIEWPORT, False),
        ("(orientation: landscape)", VIEWPORT, True),
        ("(orientation: landscape)", (400, 800), False),
        ("(prefers-color-scheme: light)", VIEWPORT, True),
        ("(prefers-color-scheme: dark)", VIEWPORT, False),
        ("(prefers-reduced-motion: no-preference)", VIEWPORT, True),
        ("(hover: hover) and (pointer: fine)", VIEWPORT, True),
        ("print, (min-width: 10px)", VIEWPORT, True),
        ("not print", VIEWPORT, True),
        ("not all and (min-width: 10px)", VIEWPORT, False),
        ("all", None, True),
        ("", VIEWPORT, True),
        ("(min-width: 10px)", None, False),
        ("(orientation: portrait)", None, False),
        ("(min-width: calc(10px))", VIEWPORT, False),
        ("(min-resolution: 2dppx)", VIEWPORT, False),
        ("tv", VIEWPORT, False),
        ("not tv", VIEWPORT, False),
    ],
)
def test_media_queries(query: str, viewport: tuple[int, int] | None, holds: bool) -> None:
    assert media_matches(query, viewport) is holds


def test_a_long_stylesheet_is_read_up_to_its_cap() -> None:
    css = ".a{color:red}" * 100_000 + ".x{display:none}"

    assert not _removed(css, ("P", {"class": "x"}))
