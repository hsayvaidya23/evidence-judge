from __future__ import annotations

import time

import pytest

from evidence_judge.ingest.css import (
    HIDDEN,
    ROOT,
    SHOWN,
    UNSURE,
    Computed,
    Element,
    Sheet,
    StyleSheets,
    matching,
    media_holds,
    supports,
)
from evidence_judge.ingest.css.colors import parse_color

VIEWPORT = (1000, 800)
Spec = tuple[str, dict[str, str]]


def _sheets(
    *css: str, viewport: tuple[int, int] | None = VIEWPORT, **kwargs: object
) -> StyleSheets:
    return StyleSheets([Sheet(c, "https://shop.test/") for c in css], viewport, **kwargs)  # pyright: ignore[reportArgumentType]


def _state_in(sheets: StyleSheets, *specs: Spec, siblings: tuple[Spec, ...] = ()) -> int:
    """How the text of the last of `specs` (each the child of the one before, the last after
    `siblings`) shows."""
    parent: Element | None = None
    style: Computed = ROOT
    chain = [("HTML", {}), ("BODY", {}), *specs]
    for depth, (tag, attrs) in enumerate(chain):
        element = Element(tag, attrs, parent)
        if depth == len(chain) - 1 and siblings:
            row = [Element(t, a, parent) for t, a in siblings]
            row.append(element)
            for i, item in enumerate(row):
                item.siblings, item.index = row, i
        style = sheets.style(element, style)
        if style.removed:
            return HIDDEN
        parent = element
    return style.text


def _state(css: str, *specs: Spec, siblings: tuple[Spec, ...] = ()) -> int:
    return _state_in(_sheets(css), *specs, siblings=siblings)


BADGE: Spec = ("SPAN", {"class": "badge in"})


@pytest.mark.parametrize(
    ("css", "element", "state"),
    [
        (".instock{display:none}", ("SPAN", {"class": "badge instock"}), HIDDEN),
        ("#stock{display:none}", ("P", {"id": "stock"}), HIDDEN),
        ("p{display:none}", ("P", {}), HIDDEN),
        ("p.a{display:none}", ("P", {"class": "b"}), SHOWN),
        ("*{display:none}", ("P", {}), HIDDEN),
        (".a.b{display:none}", ("P", {"class": "a"}), SHOWN),
        (".a.b{display:none}", ("P", {"class": "b a"}), HIDDEN),
        (".x, .y{display:none}", ("P", {"class": "y"}), HIDDEN),
        (".x{content-visibility:hidden}", ("P", {"class": "x"}), HIDDEN),
        (".x{display:none !important}.x.y{display:block}", ("P", {"class": "x y"}), HIDDEN),
        (".x{display:none}.x.y{display:block}", ("P", {"class": "x y"}), SHOWN),
        (".x{display:none}.x{display:block}", ("P", {"class": "x"}), SHOWN),
        (".x{display:block}.x{display:none}", ("P", {"class": "x"}), HIDDEN),
        ("#i{display:block}.x.y.z{display:none}", ("P", {"id": "i", "class": "x y z"}), SHOWN),
        (".tab:not(.active){display:none}", ("DIV", {"class": "tab"}), HIDDEN),
        (".tab:not(.active){display:none}", ("DIV", {"class": "tab active"}), SHOWN),
        ("[data-state=closed]{display:none}", ("DIV", {"data-state": "closed"}), HIDDEN),
        ('[aria-hidden="true"]{display:none}', ("DIV", {"aria-hidden": "true"}), HIDDEN),
        ("[data-x]{display:none}", ("DIV", {"data-x": ""}), HIDDEN),
        ("[lang|=en]{display:none}", ("DIV", {"lang": "en-GB"}), HIDDEN),
        ("[class~=b]{display:none}", ("DIV", {"class": "a b"}), HIDDEN),
        ("[href^=http]{display:none}", ("A", {"href": "https://x"}), HIDDEN),
        ("[href$='.pdf' i]{display:none}", ("A", {"href": "A.PDF"}), HIDDEN),
        ("[href*=shop]{display:none}", ("A", {"href": "/shop/x"}), HIDDEN),
        ("[href*=shop]{display:none}", ("A", {"href": "/cart"}), SHOWN),
        ("[href^=x]{display:none}", ("A", {}), SHOWN),
        (".md\\:block{display:none}", ("DIV", {"class": "md:block"}), HIDDEN),
        (".\\31 0{display:none}", ("DIV", {"class": "10"}), HIDDEN),
        (".x{display:n\\6f ne}", ("P", {"class": "x"}), HIDDEN),
        ("a:hover{display:none}", ("A", {}), UNSURE),
        ("a:focus-within{display:none}", ("A", {}), UNSURE),
        ("p:has(b){display:none}", ("P", {}), UNSURE),
        ("p:-moz-focusring, p{display:none}", ("P", {}), SHOWN),
        ("p:unknown-thing, p{display:none}", ("P", {}), UNSURE),
        ("p::before{display:none}", ("P", {}), SHOWN),
        ("p::first-line{font-size:0}", ("P", {}), UNSURE),
        ("p::part(x){display:none}", ("P", {}), SHOWN),
        ("p:first-child{display:none}", ("P", {}), HIDDEN),
        ("p:nth-child(2){display:none}", ("P", {}), SHOWN),
        ("p:only-of-type{display:none}", ("P", {}), HIDDEN),
        ("> p{display:none}", ("P", {}), SHOWN),
        ("p >{display:none}", ("P", {}), SHOWN),
        ("p:is(.a){display:none}", ("P", {"class": "a"}), HIDDEN),
        (":where(.a){display:none}", ("P", {"class": "a"}), HIDDEN),
        (":is(.a){display:none}", ("P", {"class": "b"}), SHOWN),
        ("p:not(div p){display:none}", ("P", {}), HIDDEN),
        ("div p:not(div p){display:none}", ("P", {}), SHOWN),
        ("p:not(.a{display:none}", ("P", {}), UNSURE),
        ("p$x{display:none}", ("P", {}), UNSURE),
        (".a $x .in{display:none}", BADGE, UNSURE),
        (".bad{{display:none}}", ("P", {"class": "bad"}), SHOWN),
        (".x{display: var(--d)}", ("P", {"class": "x"}), SHOWN),
        (":root{--d:none}.x{display: var(--d)}", ("P", {"class": "x"}), HIDDEN),
        (".x:hover{--d:none}.x{display: var(--d)}", ("P", {"class": "x"}), UNSURE),
        (".x{display: flexx}", ("P", {"class": "x"}), UNSURE),
        (".x{display: inline flex}", ("P", {"class": "x"}), SHOWN),
        (".x{display: revert}", ("P", {"class": "x"}), UNSURE),
        (".x{all: unset}", ("P", {"class": "x"}), SHOWN),
        (".x{color:red}", ("P", {"class": "x"}), SHOWN),
        ("/* .x{display:none} */", ("P", {"class": "x"}), SHOWN),
        ('.x::after{content:"}"}.x{display:none}', ("P", {"class": "x"}), HIDDEN),
        ("@font-face{font-family:x}.x{display:none}", ("P", {"class": "x"}), HIDDEN),
        ("@keyframes k{from{display:none}}", ("P", {}), SHOWN),
        ("@supports (display:grid){.x{display:none}}", ("P", {"class": "x"}), HIDDEN),
        ("@supports not (display:grid){.x{display:none}}", ("P", {"class": "x"}), SHOWN),
        (
            "@supports (display:grid) and (not (display:inline-grid)){.x{display:none}}",
            ("P", {"class": "x"}),
            SHOWN,
        ),
        ("@supports (display:grid) or (-moz-x:y){.x{display:none}}", ("P", {"class": "x"}), HIDDEN),
        ("@supports (gap:1px){.x{display:none}}", ("P", {"class": "x"}), UNSURE),
        ("@supports (-moz-appearance:none){.x{display:none}}", ("P", {"class": "x"}), SHOWN),
        ("@supports selector(:has(a)){.x{display:none}}", ("P", {"class": "x"}), UNSURE),
        ("@layer base{.x{display:none}}", ("P", {"class": "x"}), HIDDEN),
        ("@media (min-width:768px){.x{display:none}}", ("P", {"class": "x"}), HIDDEN),
        ("@media (max-width:768px){.x{display:none}}", ("P", {"class": "x"}), SHOWN),
        ("@media(min-width:768px){.x{display:none}}", ("P", {"class": "x"}), HIDDEN),
        ("@media print{.x{display:none}}", ("P", {"class": "x"}), SHOWN),
        ("@media (min-resolution:1dppx){.x{display:none}}", ("P", {"class": "x"}), UNSURE),
        ("@media (min-width:calc(1px)){.x{display:none}}", ("P", {"class": "x"}), HIDDEN),
        ("@media (min-width:calc(1px + 1%)){.x{display:none}}", ("P", {"class": "x"}), UNSURE),
        ("@container (min-width:1px){.x{display:none}}", ("P", {"class": "x"}), UNSURE),
        ("@scope (.p){.x{display:none}}", ("P", {"class": "x"}), UNSURE),
        ("@whatever{.x{display:none}}", ("P", {"class": "x"}), UNSURE),
        ("@starting-style{.x{display:none}}", ("P", {"class": "x"}), SHOWN),
        (".x{display:none", ("P", {"class": "x"}), HIDDEN),
        ("} .x{display:none}", ("P", {"class": "x"}), HIDDEN),
        (".x{.y{display:none}}", ("P", {"class": "y"}), SHOWN),
        (":root{display:none}", ("P", {}), HIDDEN),
        (":scope{display:none}", ("P", {}), HIDDEN),
        (":not(:defined){display:none}", ("P", {}), SHOWN),
        (":not(:defined){display:none}", ("X-CARD", {}), UNSURE),
        (":any-link{display:none}", ("A", {"href": "/"}), HIDDEN),
        (":link{display:none}", ("A", {"href": "/"}), UNSURE),
        ("p:nth-child(2 of .x){display:none}", ("P", {}), UNSURE),
        ("p:nth-child(x){display:none}", ("P", {}), SHOWN),
    ],
)
def test_rules_show_hide_or_leave_unsure(css: str, element: Spec, state: int) -> None:
    assert _state(css, element) == state


@pytest.mark.parametrize(
    ("css", "state"),
    [
        (".out ~ .in{display:none}", HIDDEN),
        (".out + .in{display:none}", SHOWN),
        ("i + .in{display:none}", HIDDEN),
        (".out ~ i ~ .in{display:none}", HIDDEN),
        ("b ~ .in{display:none}", SHOWN),
        ("i:first-child{display:none}", SHOWN),
        (".in:last-child{display:none}", HIDDEN),
        (".in:nth-child(3){display:none}", HIDDEN),
        (".in:nth-child(odd){display:none}", HIDDEN),
        (".in:nth-child(2n){display:none}", SHOWN),
        (".in:nth-last-child(1){display:none}", HIDDEN),
        (".in:nth-child(-n+2){display:none}", SHOWN),
        ("span:first-of-type{display:none}", HIDDEN),
        ("span:nth-of-type(1){display:none}", HIDDEN),
        ("i:only-of-type ~ span{display:none}", SHOWN),
        (".in:only-child{display:none}", SHOWN),
    ],
)
def test_sibling_combinators_and_structural_pseudo_classes(css: str, state: int) -> None:
    siblings: tuple[Spec, ...] = (("I", {"class": "out"}), ("I", {}))

    assert _state(css, BADGE, siblings=siblings) == state


@pytest.mark.parametrize(
    ("css", "state"),
    [
        ("@layer a{.in{display:none}} .in{display:inline}", SHOWN),
        ("@layer a{.badge.in{display:none}} .in{display:inline}", SHOWN),
        (".in{display:inline} @layer a{.in{display:none}}", SHOWN),
        ("@layer a{.in{display:inline}} @layer b{.in{display:none}}", HIDDEN),
        ("@layer b, a; @layer a{.in{display:inline}} @layer b{.in{display:none}}", SHOWN),
        ("@layer a{.in{display:none !important}} .in{display:inline !important}", HIDDEN),
        ("@layer a{.in{display:none !important}} @layer b{.in{display:inline !important}}", HIDDEN),
        ("@layer a{.in{display:inline}} @layer a.b{.in{display:none}}", SHOWN),
        ("@layer{.in{display:none}} @layer{.in{display:inline}}", SHOWN),
        ("@layer a{@layer b{.in{display:none}}} @layer a{.in{display:inline}}", SHOWN),
    ],
)
def test_cascade_layers(css: str, state: int) -> None:
    assert _state(css, BADGE) == state


@pytest.mark.parametrize(
    ("css", "state"),
    [
        (".in{opacity:0}", UNSURE),
        (".in{opacity:0.02}", UNSURE),
        (".in{opacity:4%}", UNSURE),
        (".in{opacity:.5}", SHOWN),
        (".in{opacity:var(--o)}", SHOWN),
        (".in{filter:opacity(0)}", UNSURE),
        (".in{filter:blur(2px)}", UNSURE),
        (".in{font-size:0}", UNSURE),
        (".in{font-size:1px}", UNSURE),
        (".in{font-size:0.1em}", UNSURE),
        (".in{font-size:12px}", SHOWN),
        (".in{font:0/0 a}", UNSURE),
        (".in{font:bold 14px/1.2 sans-serif}", SHOWN),
        (".in{color:transparent}", UNSURE),
        (".in{color:rgba(0,0,0,0)}", UNSURE),
        (".in{color:rgb(0 0 0 / 0%)}", UNSURE),
        (".in{color:#0000}", UNSURE),
        (".in{color:#fff}", UNSURE),
        (".in{color:white;background:black}", SHOWN),
        (".in{color:hsl(0 0% 100%)}", UNSURE),
        (".in{color:#333}", SHOWN),
        (".in{color:var(--c)}", SHOWN),
        (".in{-webkit-text-fill-color:transparent}", UNSURE),
        (
            ".in{-webkit-text-fill-color:transparent;background:linear-gradient(red,blue);"
            "-webkit-background-clip:text}",
            SHOWN,
        ),
        (".in{position:absolute;clip:rect(0 0 0 0)}", UNSURE),
        (".in{position:absolute;clip:rect(auto,auto,auto,auto)}", SHOWN),
        (".in{clip:rect(0 0 0 0)}", SHOWN),
        (".in{clip-path:inset(50%)}", UNSURE),
        (".in{clip-path:inset(0 0 100% 0)}", UNSURE),
        (".in{clip-path:inset(10%)}", SHOWN),
        (".in{clip-path:circle(0)}", UNSURE),
        (".in{clip-path:polygon(0 0, 0 0, 0 0)}", UNSURE),
        (".in{transform:scale(0)}", UNSURE),
        (".in{transform:scaleY(0.01)}", UNSURE),
        (".in{transform:scale(1.05)}", SHOWN),
        (".in{transform:translateX(-9999px)}", UNSURE),
        (".in{transform:rotateY(90deg)}", UNSURE),
        (".in{transform:matrix(0,0,0,0,0,0)}", UNSURE),
        (".in{transform:translate(var(--x))}", SHOWN),
        (".in{scale:0}", UNSURE),
        (".in{position:absolute;left:-9999px}", UNSURE),
        (".in{position:absolute;inset:-5000px auto auto auto}", UNSURE),
        (".in{position:fixed;top:-100vw}", UNSURE),
        (".in{position:absolute;left:10px}", SHOWN),
        (".in{left:-9999px}", SHOWN),
        (".in{margin-left:-9999px}", UNSURE),
        (".in{margin:-5000px 0 0}", UNSURE),
        (".in{text-indent:-9999px}", UNSURE),
        (".in{text-indent:-100%}", UNSURE),
        (".in{text-indent:2em}", SHOWN),
        (".in{height:0;overflow:hidden}", UNSURE),
        (".in{max-height:0;overflow-y:clip}", UNSURE),
        (".in{width:1px;height:1px;overflow:hidden}", UNSURE),
        (".in{height:0}", SHOWN),
        (".in{height:20px;overflow:hidden}", SHOWN),
        (".in{zoom:0.01}", UNSURE),
        ("@media (hover:hover){.in{opacity:0}}", UNSURE),
        ("a:hover .in{opacity:0}", SHOWN),
        (".x:hover .in{opacity:0}", SHOWN),
        (".in:hover{opacity:0}", UNSURE),
    ],
)
def test_styles_that_may_hide_text_leave_it_unsure(css: str, state: int) -> None:
    assert _state(css, BADGE) == state


@pytest.mark.parametrize(
    ("css", "state"),
    [
        (".in{--o:0;opacity:var(--o)}", UNSURE),
        (".in{opacity:calc(0)}", UNSURE),
        (".in{opacity:calc(1 - 0.98)}", UNSURE),
        (".in{opacity:clamp(0, 0.5, 1)}", SHOWN),
        (".in{opacity:foo(1)}", UNSURE),
        (".in{opacity:attr(data-o type(<number>))}", UNSURE),
        (".in{opacity:calc(1/0)}", UNSURE),
        (".in{transform:scale(calc(0))}", UNSURE),
        (".in{transform:scale(calc(1 / 2))}", SHOWN),
        (".in{transform:translate(-50%, -50%)}", SHOWN),
        (".in{transform:translateX(calc(-100% - 1px))}", UNSURE),
        (".in{--c:transparent;color:var(--c)}", UNSURE),
        (":root{--c:#333}.in{color:var(--c)}", SHOWN),
        (".in{color:var(--c, transparent)}", UNSURE),
        (".in{color:mystery(1 2 3)}", UNSURE),
        (".in{color:oklch(0.21 0.034 264.665)}", SHOWN),
        (".in{color:color-mix(in oklab, black 50%, transparent)}", SHOWN),
        (".in{font-size:max(0px,0px)}", UNSURE),
        (".in{font-size:clamp(1rem, 2vw, 2rem)}", SHOWN),
        (".in{font-size:var(--fs)}", SHOWN),
        (".in{font-size:larger(2)}", UNSURE),
        (".in{position:absolute;left:calc(-10000px)}", UNSURE),
        (".in{position:absolute;left:calc(50% - 20px)}", SHOWN),
        (".in{position:absolute;left:anchor(--a right)}", UNSURE),
        (".in{position:fixed;top:env(safe-area-inset-top, 0)}", SHOWN),
        (".in{position:fixed;top:env(titlebar-area-y)}", UNSURE),
        (".in{margin:var(--m)}", SHOWN),
        (":root{--m:0 0 0 -9999px}.in{margin:var(--m)}", UNSURE),
        (":root{--m:0}.x:hover{--m:-9999px}.in{margin-left:var(--m)}", SHOWN),
        (":root{--m:0}.in:hover{--m:-9999px}.in{margin-left:var(--m)}", UNSURE),
        (".in{--a:var(--b);--b:var(--a);opacity:var(--a, 0)}", UNSURE),
        (".in{--a:var(--a, 0);opacity:var(--a, 1)}", SHOWN),
        (".in{--a:var(--b) var(--b);--b:var(--c) var(--c);--c:x;opacity:var(--a)}", UNSURE),
        (
            "@property --o{syntax:'<number>';inherits:false;initial-value:0}.in{opacity:var(--o)}",
            UNSURE,
        ),
        ("@property --o{syntax:'*';inherits:true}.in{opacity:var(--o, 1)}", SHOWN),
        ("@property --o{syntax:'<number>';inherits:false}.in{opacity:var(--o, 0)}", UNSURE),
        (
            "@supports (gap:1px){@property --o{syntax:'*';inherits:true}}.in{opacity:var(--o,1)}",
            UNSURE,
        ),
        (".in{text-indent:calc(-100vw - 10px)}", UNSURE),
        (".in{text-indent:var(--i)}", SHOWN),
        (".in{width:0;overflow:hidden;width:calc(1px - 1px)}", UNSURE),
        (".in{height:calc(100vh - 60px);overflow:auto}", SHOWN),
    ],
)
def test_values_the_reader_cannot_read_leave_text_unsure(css: str, state: int) -> None:
    assert _state(css, BADGE) == state


@pytest.mark.parametrize(
    ("css", "state"),
    [
        (".in{--\\64:none;display:var(--d)}", HIDDEN),
        (":root{--\\63:transparent}.in{color:var(--c)}", UNSURE),
        (":root{--\\63:transparent}.in{color:var(--\\63)}", UNSURE),
        (":root{--c:transparent}.in{color:var(--\\63)}", UNSURE),
        (":root{--\\000063 :transparent}.in{color:var(--c)}", UNSURE),
        (":root{--a\\:b:transparent}.in{color:var(--a\\:b)}", UNSURE),
        (":root{--\\63:#000}.in{color:var(--c)}", SHOWN),
        (".in{co\\6cor:transparent}", UNSURE),
        (".in{di\\73 play:none}", HIDDEN),
        (".in{color:transparent!\\69mportant}.in{color:#000}", UNSURE),
        (
            "@property --\\63{syntax:'<color>';inherits:true;initial-value:transparent}"
            ".in{color:var(--c)}",
            UNSURE,
        ),
    ],
)
def test_escaped_property_names_read_as_the_browser_reads_them(css: str, state: int) -> None:
    assert _state(css, BADGE) == state


@pytest.mark.parametrize(
    ("css", "state"),
    [
        (".in{mask-image:linear-gradient(transparent,transparent)}", UNSURE),
        (".in{-webkit-mask-image:url(m.svg)}", UNSURE),
        (".in{-webkit-mask:none;mask:none}", SHOWN),
        (".in{filter:blur(60px)}", UNSURE),
        (".in{filter:blur(0.3px) drop-shadow(0 1px 2px #000) grayscale(1)}", SHOWN),
        (".in{filter:brightness(1.1) contrast(0.9)}", SHOWN),
        (".in{filter:brightness(100)}", UNSURE),
        (".in{filter:invert(1)}", UNSURE),
        (".in{filter:url(#f)}", UNSURE),
        (".in{filter:none}", SHOWN),
        (".in{transform:rotate3d(0,1,0,90deg)}", UNSURE),
        (".in{transform:rotate3d(0,0,1,90deg)}", SHOWN),
        (".in{rotate:y 90deg}", UNSURE),
        (".in{rotate:45deg}", SHOWN),
        (".in{rotate:y 60deg;scale:0.08}", UNSURE),
        (".in{transform:skewX(90deg)}", UNSURE),
        (".in{transform:skew(10deg, 5deg)}", SHOWN),
        (".in{transform:matrix3d(0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,1)}", UNSURE),
        (".in{transform:perspective(10px) translateZ(20px)}", UNSURE),
        (".in{transform:perspective(800px) rotateY(20deg)}", SHOWN),
        (".in{transform:wobble(1)}", UNSURE),
        (".in{transform:none}", SHOWN),
        (".in{offset-path:path('M0 0 L9999 0');offset-distance:100%}", UNSURE),
        (".in{clip-path:path('M0 0')}", UNSURE),
        (".in{clip-path:rect(0 0 0 0)}", UNSURE),
        (".in{clip-path:url(#c)}", UNSURE),
        (".in{clip-path:circle(5px)}", UNSURE),
        (".in{clip-path:circle(50%)}", SHOWN),
        (".in{clip-path:inset(10px)}", UNSURE),
        (".in{clip-path:inset(0 round 8px)}", SHOWN),
        (".in{clip-path:polygon(0 0, 100% 0, 100% calc(100% - 4px), 0 100%)}", SHOWN),
        (".in{clip-path:polygon(0 0, 100% 0, 100% 0)}", UNSURE),
        (".in{clip-path:border-box}", SHOWN),
        (".in{height:0;contain:paint}", UNSURE),
        (".in{height:0;contain:layout}", SHOWN),
        (".in{height:0;overflow:auto}", UNSURE),
        (".in{color:#fefefe}", UNSURE),
        (".in{color:snow}", UNSURE),
        (".in{color:#ccc}", SHOWN),
        (".in{color:rgba(0,0,0,0.04)}", UNSURE),
        (".in{color:#000;opacity:0.3}", SHOWN),
        (".in{background:#000;color:#0a0a0a}", UNSURE),
        (".in{background:rgba(0,0,0,0.5);color:#7f7f7f}", UNSURE),
        (".in{-webkit-text-fill-color:#fff}", UNSURE),
        (".in{color:#fff;-webkit-text-fill-color:#000}", SHOWN),
        (".in{position:fixed;top:-100vh}", UNSURE),
        (".in{position:fixed;top:100vh}", UNSURE),
        (".in{position:fixed;top:-60px}", SHOWN),
        (".in{position:absolute;inset-inline-start:-10000px}", UNSURE),
        (".in{position:absolute;inset-block:-9999px auto}", UNSURE),
        (".in{margin-inline:-9999px 0}", UNSURE),
        (".in{text-indent:100%;white-space:nowrap;overflow:hidden}", UNSURE),
        (".in{text-indent:5%}", SHOWN),
    ],
)
def test_hiding_the_reader_did_not_model_leaves_text_unsure(css: str, state: int) -> None:
    assert _state(css, BADGE) == state


@pytest.mark.parametrize(
    ("css", "state"),
    [
        (".wrap{opacity:0.2}.in{opacity:0.2}", UNSURE),
        (".wrap{opacity:0.5}.in{opacity:0.5}", SHOWN),
        (".wrap{color:#000}.wrap:hover{color:#fefefe}", UNSURE),
        (".wrap{color:#000}.x:hover{color:#fefefe}", SHOWN),
        (".wrap{background-image:url(x.png)}.in{color:#fff}", SHOWN),
    ],
)
def test_faint_text_through_its_ancestors(css: str, state: int) -> None:
    assert _state(css, ("DIV", {"class": "wrap"}), BADGE) == state


@pytest.mark.parametrize(
    ("attrs", "state"),
    [
        ({}, SHOWN),
        ({"font-size": "0"}, UNSURE),
        ({"font-size": "12"}, SHOWN),
        ({"transform": "scale(0)"}, UNSURE),
        ({"transform": "translate(10 20) rotate(30 5 5)"}, SHOWN),
        ({"transform": "translate(-20000, 0)"}, UNSURE),
        ({"clip-path": "url(#c)"}, UNSURE),
        ({"mask": "url(#m)"}, UNSURE),
        ({"filter": "url(#f)"}, UNSURE),
        ({"fill": "white"}, UNSURE),
        ({"fill": "#333"}, SHOWN),
        ({"fill": "url(#g)"}, UNSURE),
        ({"fill": "currentColor", "color": "white"}, UNSURE),
        ({"opacity": "0"}, UNSURE),
    ],
)
def test_svg_presentation_attributes_read_like_css(attrs: dict[str, str], state: int) -> None:
    svg: Spec = ("svg", {"width": "300", "height": "40"})
    parent: Element | None = None
    style: Computed = ROOT
    sheets = _sheets("")
    for tag, given, namespace in (
        ("HTML", {}, "html"),
        ("BODY", {}, "html"),
        (*svg, "svg"),
        ("text", {"x": "0", "y": "20", **attrs}, "svg"),
    ):
        element = Element(tag, given, parent, namespace=namespace)
        style = sheets.style(element, style)
        parent = element

    assert style.text == state


@pytest.mark.parametrize(
    ("style", "state"),
    [
        ("opacity:1;opacity:0", UNSURE),
        ("opacity:0;opacity:1", SHOWN),
        ("margin:var(--m);margin-left:0", SHOWN),
        ("margin-left:0;margin:var(--m)", UNSURE),
    ],
)
def test_the_last_inline_declaration_wins(style: str, state: int) -> None:
    assert _state(":root{--m:0 0 0 -9999px}", ("SPAN", {"style": style})) == state


@pytest.mark.parametrize(
    ("css", "state"),
    [
        (".wrap{opacity:0}", UNSURE),
        (".wrap{font-size:0}", UNSURE),
        (".wrap{font-size:0}.in{font-size:16px}", SHOWN),
        (".wrap{font-size:0}.in{font-size:2em}", UNSURE),
        (".wrap{color:transparent}", UNSURE),
        (".wrap{color:transparent}.in{color:black}", SHOWN),
        (".wrap{background:#000}.in{color:#000}", UNSURE),
        (".wrap{background:#000;color:#fff}", SHOWN),
        (".wrap{background:url(x.png)}.in{color:#fff}", SHOWN),
        (".wrap{text-indent:-9999px}", UNSURE),
        (".wrap{text-indent:-9999px}.in{text-indent:0}", SHOWN),
        (".wrap{height:0;overflow:hidden}", UNSURE),
        (".wrap{visibility:hidden}", HIDDEN),
        (".wrap{visibility:hidden}.in{visibility:visible}", SHOWN),
        (".wrap{visibility:hidden}.in{visibility:var(--v)}", HIDDEN),
        (".wrap{visibility:hidden}.wrap:hover{--v:visible}.in{visibility:var(--v,hidden)}", UNSURE),
        (".wrap{--o:0}.in{opacity:var(--o)}", UNSURE),
        (
            "@property --o{syntax:'<number>';inherits:false;initial-value:1}"
            ".wrap{--o:0}.in{opacity:var(--o)}",
            SHOWN,
        ),
        (".wrap{--o:0}.in{--o:initial;opacity:var(--o, 1)}", SHOWN),
        (".wrap{--o:0}.in{--o:inherit;opacity:var(--o, 1)}", UNSURE),
        (".wrap:hover{visibility:hidden}", UNSURE),
        (".wrap{visibility:hidden}.wrap:hover{visibility:visible}", UNSURE),
        (".wrap{display:none}.wrap:hover{display:block}", UNSURE),
    ],
)
def test_inherited_and_subtree_effects(css: str, state: int) -> None:
    assert _state(css, ("DIV", {"class": "wrap"}), BADGE) == state


def test_root_matches_only_the_document_element() -> None:
    sheets = _sheets(":root{display:none}")

    assert sheets.style(Element("HTML", {}, None), ROOT).removed
    assert not sheets.style(Element("HTML", {}, Element("DIV", {}, None)), ROOT).removed


@pytest.mark.parametrize(
    ("css", "state"),
    [
        (".card .price{display:none}", HIDDEN),
        (".shop .price{display:none}", SHOWN),
        ("main > .price{display:none}", SHOWN),
        (".card > .price{display:none}", HIDDEN),
        ("main .card > .price{display:none}", HIDDEN),
        ("body > .price{display:none}", SHOWN),
        ("main div.card .price{display:none}", HIDDEN),
        (".card{.price{display:none}}", HIDDEN),
        (".card{> .price{display:none}}", HIDDEN),
        (".card{&.open .price{display:none}}", SHOWN),
        (".card{& .price{display:none}}", HIDDEN),
        (".card{&{display:none}}", HIDDEN),
        (".card{.x & {display:none}}", UNSURE),
        (".card{& & .price{display:none}}", UNSURE),
        (".card{&:hover .price{display:none}}", UNSURE),
        (".card{@media (min-width:1px){.price{display:none}}}", HIDDEN),
        ("main{.card{.price{display:none}}}", HIDDEN),
        (".a, .card{.price{display:none}}", HIDDEN),
    ],
)
def test_combinators_and_nesting(css: str, state: int) -> None:
    price: tuple[Spec, ...] = (
        ("MAIN", {}),
        ("DIV", {"class": "card"}),
        ("SPAN", {"class": "price"}),
    )

    assert _state(css, *price) == state


def _deep(sheets: StyleSheets, depth: int, top: str) -> int:
    specs: list[Spec] = [(top, {})] + [("DIV", {"class": "a"})] * depth + [("SPAN", {})]
    return _state_in(sheets, *specs)


@pytest.mark.parametrize(
    ("css", "top", "state"),
    [
        ("section div div div div span{display:none}", "SECTION", HIDDEN),
        ("section div div div div span{display:none}", "MAIN", SHOWN),
        ("section > div div div > div span{display:none}", "SECTION", HIDDEN),
        ("main > div div span{display:none}", "SECTION", SHOWN),
        ("section div:hover div span{display:none}", "SECTION", UNSURE),
        ("section div:hover div span{display:none}", "MAIN", SHOWN),
    ],
)
def test_descendant_chains_on_a_deep_tree(css: str, top: str, state: int) -> None:
    assert _deep(_sheets(css), 12, top) == state


def test_a_match_past_its_step_budget_is_unsure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(matching, "MAX_MATCH_STEPS", 5)

    assert _deep(_sheets("section div div div span{display:none}"), 20, "SECTION") == UNSURE


@pytest.mark.perf
@pytest.mark.parametrize("top", ["SECTION", "MAIN"])
def test_descendant_chains_match_a_deep_tree_within_budget(top: str) -> None:
    chain = " ".join(["div"] * 12)
    sheets = _sheets(f"section {chain}{{display:none}} div > section {chain} span{{opacity:0}}")
    start = time.perf_counter()

    state = _deep(sheets, 1000, top)

    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"took {elapsed:.2f}s"
    assert state == (HIDDEN if top == "SECTION" else SHOWN)


def test_nested_ampersand_joins_the_parent_compound() -> None:
    css = ".card{display:block;&.closed{display:none}}"

    assert _state(css, ("DIV", {"class": "card closed"})) == HIDDEN
    assert _state(css, ("DIV", {"class": "card"})) == SHOWN


@pytest.mark.parametrize("classes", ["hidden", "d-none", "sr-only", "visually-hidden", "invisible"])
def test_utility_classes_without_their_stylesheet_are_unsure(classes: str) -> None:
    assert _state("", ("SPAN", {"class": classes})) == UNSURE


def test_a_stylesheet_that_defines_a_utility_class_decides_it() -> None:
    assert _state(".hidden{display:none}", ("SPAN", {"class": "hidden"})) == HIDDEN
    assert _state(".hidden{color:red}", ("SPAN", {"class": "hidden"})) == SHOWN
    shown = ".hidden{display:none}@media (min-width:768px){.md\\:block{display:block}}"
    assert _state(shown, ("NAV", {"class": "hidden md:block"})) == SHOWN
    narrow = _sheets(shown, viewport=(400, 800))
    assert _state_in(narrow, ("NAV", {"class": "hidden md:block"})) == HIDDEN


def test_utility_guesses_apply_only_to_the_document() -> None:
    shadow = StyleSheets([], VIEWPORT, document=False)

    assert _state_in(shadow, ("SPAN", {"class": "hidden"})) == SHOWN


def test_inline_styles_win_over_sheets_unless_important() -> None:
    sheets = _sheets(".x{display:none}.y{display:none!important}")

    assert not sheets.style(
        Element("P", {"class": "x", "style": "display:inline"}, None), ROOT
    ).removed
    assert sheets.style(Element("P", {"class": "y", "style": "display:inline"}, None), ROOT).removed
    assert sheets.style(Element("P", {"style": "display : NONE"}, None), ROOT).removed
    assert not sheets.style(Element("P", {"style": "content:'display:none'"}, None), ROOT).removed


@pytest.mark.parametrize(
    ("css", "hidden", "state"),
    [
        ("", "", HIDDEN),
        ("[hidden]{display:inline !important}", "", SHOWN),
        (".x{display:flex}", "", SHOWN),
        (".x:hover{display:flex}", "", UNSURE),
        ("", "until-found", UNSURE),
        (".x{display:block}", "until-found", UNSURE),
        (".x{content-visibility:visible}", "until-found", SHOWN),
    ],
)
def test_the_browsers_own_hiding_yields_to_the_page(css: str, hidden: str, state: int) -> None:
    sheets = _sheets(css)
    element = Element("P", {"class": "x", "hidden": hidden}, None)
    style = sheets.style(element, ROOT)

    assert (HIDDEN if style.removed else style.text) == state


@pytest.mark.parametrize(
    ("css", "state"),
    [
        (".x{visibility:hidden}", HIDDEN),
        (".x{visibility:collapse}", HIDDEN),
        (".x{visibility:visible}", SHOWN),
        (".x{visibility:inherit}", SHOWN),
        (".x{visibility:unset}", SHOWN),
        (".x{color:red}", SHOWN),
        (".invisible-ish{visibility:hidden}", SHOWN),
    ],
)
def test_visibility(css: str, state: int) -> None:
    style = _sheets(css).style(Element("P", {"class": "x"}, None), ROOT)

    assert style.text == state
    assert not style.removed


def _imports(sheets: dict[str, str]) -> StyleSheets:
    return StyleSheets(
        [Sheet('@import url("/a.css"); .z{color:red}', "https://shop.test/p/1")],
        VIEWPORT,
        source=sheets.get,
    )


def test_imported_sheets_apply_from_the_recording() -> None:
    sheets = _imports({"https://shop.test/a.css": ".in{display:none}"})

    assert _state_in(sheets, BADGE) == HIDDEN
    assert not sheets.unknown


def test_an_import_the_recording_does_not_hold_leaves_the_scope_unknown() -> None:
    assert _imports({}).unknown
    assert _sheets("@import url(x.css);").unknown


@pytest.mark.parametrize(
    ("statement", "state"),
    [
        ('@import "b.css";', HIDDEN),
        ("@import url(b.css) print;", SHOWN),
        ("@import url(b.css) (min-width: 1px);", HIDDEN),
        ("@import url(b.css) (min-resolution: 2dppx);", UNSURE),
        ("@import url(b.css) supports(display:grid);", HIDDEN),
        ("@import url(b.css) supports(gap:1px);", UNSURE),
        ("@import url(b.css) layer(base); .in{display:inline}", SHOWN),
        ("@import url(b.css) layer; .in{display:inline}", SHOWN),
        ("@import url(b.css); .in{display:inline}", SHOWN),
    ],
)
def test_import_conditions_and_layers(statement: str, state: int) -> None:
    sheets = StyleSheets(
        [Sheet(statement, "https://shop.test/css/main.css")],
        VIEWPORT,
        source={"https://shop.test/css/b.css": ".in{display:none}"}.get,
    )

    assert _state_in(sheets, BADGE) == state


def test_import_cycles_stop() -> None:
    sheets = StyleSheets(
        [Sheet("@import 'a.css';", "https://shop.test/")],
        VIEWPORT,
        source={"https://shop.test/a.css": "@import 'a.css'; .in{display:none}"}.get,
    )

    assert sheets.unknown
    assert _state_in(sheets, BADGE) == UNSURE or sheets.unknown


def test_a_sheet_under_an_unreadable_media_attribute_may_apply() -> None:
    sheets = StyleSheets([Sheet(".in{display:none}", None, maybe=True)], VIEWPORT)

    assert _state_in(sheets, BADGE) == UNSURE


def test_scope_flags_for_parts_slots_and_details() -> None:
    sheets = _sheets(
        "x-a::part(label){display:none}",
        "::slotted(span){visibility:hidden}",
        "details::details-content{content-visibility:visible}",
    )

    assert (sheets.parts, sheets.slotted, sheets.details) == (True, True, True)
    assert _sheets("::slotted(span){color:red;font-weight:bold}").slotted is True
    assert _sheets("p::before{content:'x'}").parts is False


def test_host_rules_style_the_host_from_its_shadow_root() -> None:
    page = _sheets(".card{display:block}")
    host = Element("X-CARD", {"class": "card"}, None)
    proxy = Element("X-CARD", host.attrs, None, host=host)
    for css, state in [
        (":host{display:none}", SHOWN),
        (":host{display:none !important}", HIDDEN),
        (":host(.card){visibility:hidden}", HIDDEN),
        (":host(.other){visibility:hidden}", SHOWN),
        (":host-context(.dark){visibility:hidden}", UNSURE),
        (".card{visibility:hidden}", SHOWN),
    ]:
        shadow = StyleSheets([Sheet(css)], VIEWPORT, document=False)
        style = page.style(host, ROOT, host=shadow, proxy=proxy)
        assert (HIDDEN if style.removed else style.text) == state, css


def test_host_rules_reach_into_the_shadow_tree() -> None:
    host = Element("X-CARD", {}, None)
    proxy = Element("X-CARD", {}, None, host=host)
    inner = Element("SPAN", {"class": "in"}, proxy)
    shadow = StyleSheets([Sheet(":host .in{display:none}*{color:red}")], VIEWPORT, document=False)

    assert shadow.style(inner, ROOT).removed
    assert shadow.style(proxy, ROOT) is ROOT


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
        ("(scripting: enabled) and (color)", VIEWPORT, True),
        ("(forced-colors: active)", VIEWPORT, False),
        ("print, (min-width: 10px)", VIEWPORT, True),
        ("not print", VIEWPORT, True),
        ("not all and (min-width: 10px)", VIEWPORT, False),
        ("all", None, True),
        ("", VIEWPORT, True),
        ("(min-width: 10px)", None, None),
        ("(orientation: portrait)", None, None),
        ("(min-width: calc(10px))", VIEWPORT, True),
        ("(min-width: calc(10px + 1%))", VIEWPORT, None),
        ("(min-resolution: 2dppx)", VIEWPORT, None),
        ("not (min-resolution: 2dppx)", VIEWPORT, None),
        ("print and (min-resolution: 2dppx)", VIEWPORT, False),
        ("(min-resolution: 2dppx), (min-width: 10px)", VIEWPORT, True),
        ("(min-width: 10vw)", VIEWPORT, None),
        ("tv", VIEWPORT, False),
        ("not tv", VIEWPORT, True),
    ],
)
def test_media_queries(query: str, viewport: tuple[int, int] | None, holds: bool | None) -> None:
    assert media_holds(query, viewport) is holds


@pytest.mark.parametrize(
    ("condition", "holds"),
    [
        ("(display: grid)", True),
        ("(display: flex) and (display: contents)", True),
        ("not (display: grid)", False),
        ("not ((display: grid))", False),
        ("(display: grid) or (gap: 1px)", True),
        ("(gap: 1px) and (display: grid)", None),
        ("(-moz-appearance: none)", False),
        ("(-moz-appearance: none) and (gap: 1px)", False),
        ("selector(:has(a))", None),
        ("font-tech(color-COLRv1)", None),
    ],
)
def test_supports_conditions(condition: str, holds: bool | None) -> None:
    assert supports(condition) is holds


def test_a_stylesheet_over_its_cap_leaves_the_scope_unknown() -> None:
    from evidence_judge.ingest.css import MAX_SHEET_CHARS

    css = ".a{color:red}" * (MAX_SHEET_CHARS // 13 + 1) + ".x{display:none}"
    sheets = _sheets(css)

    assert sheets.unknown
    assert _state_in(sheets, ("P", {"class": "x"})) == SHOWN


@pytest.mark.parametrize(
    ("text", "rgba"),
    [
        ("snow", (255, 250, 250, 1.0)),
        ("#abc", (170, 187, 204, 1.0)),
        ("rgb(10 20 30 / 50%)", (10, 20, 30, 0.5)),
        ("rgba(0,0,0,0)", (0, 0, 0, 0.0)),
        ("hsl(0 0% 100%)", (255, 255, 255, 1.0)),
        ("hsla(120, 100%, 25%, .5)", (0, 128, 0, 0.5)),
        ("hwb(0 100% 0%)", (255, 255, 255, 1.0)),
        ("oklch(1 0 0)", (255, 255, 255, 1.0)),
        ("oklab(0 0 0 / 0.2)", (0, 0, 0, 0.2)),
        ("lab(100 0 0)", (255, 255, 255, 1.0)),
        ("lch(0% 0 0)", (0, 0, 0, 1.0)),
        ("color(srgb 1 1 1)", (255, 255, 255, 1.0)),
        ("color-mix(in srgb, white 50%, black)", (128, 128, 128, 1.0)),
        ("color-mix(in oklab, black 50%, transparent)", (0, 0, 0, 0.5)),
        ("light-dark(white, black)", (255, 255, 255, 1.0)),
        ("canvastext", (0, 0, 0, 1.0)),
        ("currentcolor", (1, 2, 3, 1.0)),
        ("rgb(from red r g b)", None),
        ("color(rec2020 1 1 1)", None),
        ("mystery", None),
    ],
)
def test_colours(text: str, rgba: tuple[float, float, float, float] | None) -> None:
    found = parse_color(text, (1, 2, 3, 1.0))

    if rgba is None:
        assert found is None
    else:
        assert found is not None
        assert all(abs(a - b) <= 1 for a, b in zip(found[:3], rgba[:3], strict=True))
        assert found[3] == pytest.approx(rgba[3])
