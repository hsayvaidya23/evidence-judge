"""The browser's own hiding rules (ingest/ua_hiding.css) and the SVG and MathML rendering rules
the page-text reader applies."""

from __future__ import annotations

import re
from importlib import resources

import pytest

from evidence_judge.ingest.css import HIDDEN, SHOWN, UNSURE
from evidence_judge.ingest.dom import FrameSnapshotStore, RenderedPage

PHRASE = "In stock"


def _rendered(*children: object) -> RenderedPage:
    store = FrameSnapshotStore()
    store.add(
        "frame@1",
        ["HTML", {}, ["HEAD", {}], ["BODY", {}, *children, ["P", {}, "Kettle"]]],
    )
    return store.render("frame@1", 0, viewport=(1000, 800), url="https://shop.test/p/kettle")


def _state(page: RenderedPage) -> int:
    if PHRASE.lower() in page.text.lower():
        return SHOWN
    return UNSURE if PHRASE.lower() in page.unsure_text.lower() else HIDDEN


def _svg(*children: object, **attrs: str) -> list[object]:
    return ["svg", {"width": "200", "height": "40", **attrs}, *children]


def _text(**attrs: str) -> list[object]:
    return ["text", {"x": "0", "y": "20", **attrs}, PHRASE]


def _in(name: str, **attrs: str) -> list[object]:
    return [name, attrs, PHRASE]


UA_CASES: dict[str, tuple[list[object], int]] = {
    "html:area": (_in("AREA"), HIDDEN),
    "html:base": (_in("BASE"), HIDDEN),
    "html:basefont": (_in("BASEFONT"), HIDDEN),
    "html:datalist": (["DATALIST", {}, _in("OPTION")], HIDDEN),
    "html:head": (_in("HEAD"), HIDDEN),
    "html:link": (_in("LINK"), HIDDEN),
    "html:meta": (_in("META"), HIDDEN),
    "html:noembed": (_in("NOEMBED"), HIDDEN),
    "html:noframes": (_in("NOFRAMES"), HIDDEN),
    "html:param": (_in("PARAM"), HIDDEN),
    "html:rp": (["RUBY", {}, "K", _in("RP"), ["RT", {}, "k"]], HIDDEN),
    "html:script": (_in("SCRIPT"), HIDDEN),
    "html:style": (_in("STYLE"), HIDDEN),
    "html:template": (_in("TEMPLATE"), HIDDEN),
    "html:title": (_in("TITLE"), HIDDEN),
    "html:embed": (_in("EMBED", hidden=""), HIDDEN),
    "html:noscript": (_in("NOSCRIPT"), HIDDEN),
    "html:input": (["INPUT", {"type": "hidden", "value": PHRASE}], HIDDEN),
    "html:dialog": (_in("DIALOG"), HIDDEN),
    "html:form": (["TABLE", {}, ["TR", {}, _in("FORM")]], HIDDEN),
    "html:table": (["TABLE", {}, _in("FORM")], HIDDEN),
    "html:thead": (["TABLE", {}, ["THEAD", {}, _in("FORM")]], HIDDEN),
    "html:tbody": (["TABLE", {}, ["TBODY", {}, _in("FORM")]], HIDDEN),
    "html:tfoot": (["TABLE", {}, ["TFOOT", {}, _in("FORM")]], HIDDEN),
    "html:tr": (["TABLE", {}, ["TR", {}, _in("FORM")]], HIDDEN),
    "html:[hidden]": (_in("SPAN", hidden=""), HIDDEN),
    "html:[hidden=until-found]": (_in("SPAN", hidden="until-found"), UNSURE),
    "html:[type]": (["INPUT", {"type": "HIDDEN", "value": PHRASE}], HIDDEN),
    "html:[open]": (_in("DIALOG", open=""), SHOWN),
    "html:[popover]": (_in("DIV", popover=""), UNSURE),
    "svg:clippath": (_svg(["clipPath", {}, _text()]), HIDDEN),
    "svg:defs": (_svg(["defs", {}, _text()]), HIDDEN),
    "svg:desc": (_svg(["desc", {}, PHRASE]), HIDDEN),
    "svg:lineargradient": (_svg(["linearGradient", {}, _text()]), HIDDEN),
    "svg:marker": (_svg(["marker", {}, _text()]), HIDDEN),
    "svg:mask": (_svg(["mask", {}, _text()]), HIDDEN),
    "svg:metadata": (_svg(["metadata", {}, PHRASE]), HIDDEN),
    "svg:pattern": (_svg(["pattern", {}, _text()]), HIDDEN),
    "svg:radialgradient": (_svg(["radialGradient", {}, _text()]), HIDDEN),
    "svg:script": (_svg(["script", {}, PHRASE]), HIDDEN),
    "svg:style": (_svg(["style", {}, PHRASE]), HIDDEN),
    "svg:symbol": (_svg(["symbol", {"id": "s"}, _text()]), HIDDEN),
    "svg:title": (_svg(["title", {}, PHRASE]), HIDDEN),
    "svg:filter": (_svg(["filter", {}, _text()]), HIDDEN),
    "math:semantics": (
        ["math", {}, ["semantics", {}, ["mi", {}, "x"], ["annotation", {}, PHRASE]]],
        HIDDEN,
    ),
    "math:maction": (["math", {}, ["maction", {}, ["mi", {}, "x"], ["mtext", {}, PHRASE]]], HIDDEN),
    "math:mphantom": (["math", {}, ["mphantom", {}, ["mtext", {}, PHRASE]]], HIDDEN),
    "math:annotation": (["math", {}, ["annotation", {}, PHRASE]], UNSURE),
    "math:annotation-xml": (["math", {}, ["annotation-xml", {}, PHRASE]], UNSURE),
}
"""For each element and attribute the browser's own hiding rules name, a page using it and
whether its text then shows."""

_NAMESPACES = {
    "http://www.w3.org/1999/xhtml": "html",
    "http://www.w3.org/2000/svg": "svg",
    "http://www.w3.org/1998/Math/MathML": "math",
}
_SELECTOR_NAME = re.compile(r"(?:^|[\s,>(])([a-zA-Z][\w-]*)(?=[\s,{>:\[)]|$)")
_ATTRIBUTE = re.compile(r"\[([\w-]+)(=[\w-]+)?")


def _named() -> set[str]:
    text = resources.files("evidence_judge.ingest").joinpath("ua_hiding.css").read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    parts = re.split(r'@namespace\s+"([^"]+)"\s*;', text)
    found: set[str] = set()
    for uri, body in zip(parts[1::2], parts[2::2], strict=True):
        namespace = _NAMESPACES[uri]
        for prelude in re.findall(r"([^{}]+)\{[^{}]*\}", body):
            if prelude.strip().startswith("@"):
                continue
            found |= {f"{namespace}:{n.lower()}" for n in _SELECTOR_NAME.findall(prelude)}
            found |= {
                f"{namespace}:[{a}{'=' + v[1:] if v and v[1:] != 'hidden' else ''}]"
                for a, v in _ATTRIBUTE.findall(prelude)
            }
    for prelude in re.findall(r"@media[^{]*\{\s*([^{}]+)\{", text):
        found |= {f"html:{n.lower()}" for n in _SELECTOR_NAME.findall(prelude)}
    return found


def test_every_element_and_attribute_the_browsers_rules_name_is_handled() -> None:
    assert _named() - set(UA_CASES) == set()


@pytest.mark.parametrize(("case", "state"), [(k, v[1]) for k, v in UA_CASES.items()])
def test_the_browsers_own_rules_decide_what_shows(case: str, state: int) -> None:
    page = _rendered(UA_CASES[case][0])

    assert _state(page) == state
    assert "Kettle" in page.text


@pytest.mark.parametrize(
    ("markup", "state"),
    [
        (["DIV", {"popover": "", "class": "p"}, PHRASE], UNSURE),
        (["DIALOG", {"popover": "", "open": ""}, PHRASE], SHOWN),
        (["DIALOG", {"__playwright_dialog_open_": "true"}, PHRASE], SHOWN),
        (["DIV", {"inert": ""}, PHRASE], SHOWN),
        (["DIV", {"style": "content-visibility:auto"}, PHRASE], SHOWN),
        (["DETAILS", {"name": "faq"}, ["SUMMARY", {}, "Stock"], ["P", {}, PHRASE]], HIDDEN),
        (["DETAILS", {"name": "faq", "open": ""}, ["SUMMARY", {}, "Q"], ["P", {}, PHRASE]], SHOWN),
        (["symbol", {}, PHRASE], SHOWN),
        (["mphantom", {}, PHRASE], SHOWN),
        (["defs", {}, PHRASE], SHOWN),
    ],
)
def test_html_states_and_names_outside_their_namespace(markup: list[object], state: int) -> None:
    assert _state(_rendered(markup)) == state


@pytest.mark.parametrize(
    ("markup", "state"),
    [
        (_svg(_text()), SHOWN),
        (_svg(_text(display="none")), HIDDEN),
        (_svg(_text(visibility="hidden")), HIDDEN),
        (_svg(_text(opacity="0")), UNSURE),
        (_svg(_text(**{"fill-opacity": "0"})), UNSURE),
        (_svg(_text(fill="none")), UNSURE),
        (_svg(_text(fill="transparent")), UNSURE),
        (_svg(["g", {"fill-opacity": "0"}, _text(fill="red")]), UNSURE),
        (_svg(["g", {"fill": "none"}, _text(fill="red")]), SHOWN),
        (_svg(_text(display="none", style="display:inline")), SHOWN),
        (["DIV", {"style": "fill:none"}, PHRASE], SHOWN),
        (["DIV", {"display": "none"}, PHRASE], SHOWN),
    ],
)
def test_svg_presentation_attributes_feed_the_cascade(markup: list[object], state: int) -> None:
    assert _state(_rendered(markup)) == state


def test_page_styles_beat_svg_presentation_attributes() -> None:
    page = _rendered(["STYLE", {}, "text{visibility:visible}"], _svg(_text(visibility="hidden")))

    assert _state(page) == SHOWN


@pytest.mark.parametrize(
    ("markup", "state"),
    [
        (_svg(["symbol", {"id": "s"}, _text()], ["use", {"href": "#s"}]), UNSURE),
        (_svg(["defs", {}, _text(id="t")], ["use", {"xlink:href": "#t"}]), UNSURE),
        (_svg(["defs", {}, ["symbol", {"id": "s"}, _text()]], ["use", {"href": "#s"}]), UNSURE),
        (_svg(["pattern", {"id": "p"}, _text()], ["rect", {"fill": "url(#p)"}]), UNSURE),
        (
            _svg(["mask", {"id": "m"}, _text()], ["rect", {"style": "mask: url('#m')"}]),
            UNSURE,
        ),
        (
            [
                "DIV",
                {},
                ["STYLE", {}, ".r{mask:url(#m)}"],
                _svg(["mask", {"id": "m"}, _text()], ["rect", {"class": "r"}]),
            ],
            UNSURE,
        ),
        (_svg(["symbol", {"id": "s"}, _text()], ["use", {"href": "#other"}]), HIDDEN),
        (
            _svg(
                ["symbol", {"id": "s", "style": "display:none"}, _text()], ["use", {"href": "#s"}]
            ),
            HIDDEN,
        ),
    ],
)
def test_what_an_svg_reference_copies_onto_the_page_is_unsure(
    markup: list[object], state: int
) -> None:
    assert _state(_rendered(markup)) == state


@pytest.mark.parametrize(
    ("markup", "state"),
    [
        (_svg(PHRASE), HIDDEN),
        (_svg(["g", {}, PHRASE]), HIDDEN),
        (_svg(["text", {}, ["tspan", {}, PHRASE]]), SHOWN),
        (_svg(["foreignObject", {}, ["DIV", {}, PHRASE]]), SHOWN),
        (_svg(["switch", {}, _text()]), SHOWN),
        (_svg(["switch", {}, ["text", {}, "Out of"], _text()]), UNSURE),
        (_svg(["switch", {}, _text(systemLanguage="fr")]), UNSURE),
        (_svg(_text(x="500", y="500"), width="10", height="10"), UNSURE),
        (_svg(_text(x="5", y="20"), width="10", height="10"), SHOWN),
        (_svg(_text(x="250"), viewBox="0 0 200 40", width="100%"), UNSURE),
        (_svg(_text(x="150"), viewBox="0 0 200 40", width="100%"), SHOWN),
        (_svg(_text(x="900"), width="100%"), SHOWN),
    ],
)
def test_svg_text_renders_inside_text_one_switch_child_and_the_canvas(
    markup: list[object], state: int
) -> None:
    assert _state(_rendered(markup)) == state
