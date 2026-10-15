"""Echoes of agent input that differ from what was typed still prove nothing.

Each run shows "Order A77 confirmed" on the final page only because the site repeats something
the agent supplied, in a form that is not an exact copy of the recorded action value.
"""

from __future__ import annotations

import pytest

from evidence_judge.checks.claims import judge_claim
from evidence_judge.checks.proof import guarded
from evidence_judge.checks.registry import default_registry
from evidence_judge.config import Settings
from evidence_judge.core.claims import Claim, ClaimKind
from evidence_judge.core.echo import MASK
from evidence_judge.core.evidence import ActionEvent, NetworkEvent, Run
from evidence_judge.core.index import EvidenceIndex, is_short_word
from evidence_judge.core.predicates import (
    PageTextPredicate,
    Predicate,
    RequestPredicate,
    UrlPredicate,
    ValuePredicate,
)
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import HOST, act, ctx, final, make_run, net, snap

CLAIM = Claim(
    id="c1",
    text="Order A77 confirmed",
    quote="Order A77 confirmed",
    kind=ClaimKind.OUTCOME,
    key=True,
    predicates=[PageTextPredicate(contains="Order A77 confirmed")],
)
ECHO_REASON = "it holds only where a page, URL or response echoes the agent's own input"
JUDGE = Judge(Settings(anthropic_api_key=None, cache_dir=None))


def _with_text(upload: ActionEvent) -> ActionEvent:
    """An upload whose file text the recording holds, so only its name can be echoed."""
    return upload.model_copy(update={"file_text": "Invoice total 12.00"})


def _run(action: ActionEvent, final_text: str) -> Run:
    return make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), action],
        network=[net(1, "GET", "/"), net(5, "GET", "/profile")],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=final("/profile", final_text),
    )


ECHOES = {
    "typed exactly": (
        act(2, "fill", "#q", value="Order A77 confirmed"),
        "You wrote Order A77 confirmed",
    ),
    "uploaded file name": (
        _with_text(act(2, "upload", "#file", value="Order A77 confirmed.pdf")),
        "Uploaded: Order A77 confirmed.pdf",
    ),
    "uploaded file stem from a path": (
        _with_text(act(2, "upload_file", "#file", value="/tmp/run/Order A77 confirmed.pdf")),
        "Your document Order A77 confirmed was received",
    ),
    "pressed text (browser-use send_keys)": (
        act(2, "send_keys", "#q", value="Order A77 confirmed"),
        "You wrote Order A77 confirmed",
    ),
    "typed markup rendered": (
        act(2, "fill", "#bio", value="<b>Order A77</b> confirmed"),
        "Bio: Order A77 confirmed",
    ),
    "typed block markup rendered": (
        act(2, "fill", "#bio", value="<p>Order A77</p><p>confirmed</p>"),
        "Bio: Order A77\nconfirmed",
    ),
    "typed character references decoded": (
        act(2, "fill", "#bio", value="Order&#32;A77 confirmed"),
        "Bio: Order A77 confirmed",
    ),
    "typed markup with a comment": (
        act(2, "fill", "#bio", value="Order A77 <!-- x --> confirmed"),
        "Bio: Order A77 confirmed",
    ),
    "typed markup with hidden text": (
        act(2, "fill", "#bio", value="Order A77<span hidden>x</span> confirmed"),
        "Bio: Order A77 confirmed",
    ),
    "typed markup hidden by its style": (
        act(2, "fill", "#bio", value="Order A77 <b style=display:none>x</b>confirmed"),
        "Bio: Order A77 confirmed",
    ),
    "echo cut short by maxlength": (
        act(2, "fill", "#name", value="Order A77 confirmed!!!!!!"),
        "Hello Order A77 confirmed",
    ),
}

GENUINE = {
    "typed text shares only a short stem": (
        act(2, "fill", "#q", value="#A77 confirmation"),
        "Order A77 confirmed",
    ),
    "key chord press": (
        act(2, "press", "#q", value="Control+Enter"),
        "Order A77 confirmed",
    ),
    "unrelated upload": (
        _with_text(act(2, "upload", "#file", value="receipt.pdf")),
        "Order A77 confirmed",
    ),
}


@pytest.mark.parametrize("case", ECHOES)
async def test_echo_of_agent_input_is_no_evidence(case: str) -> None:
    action, final_text = ECHOES[case]
    verdict = await judge_claim(CLAIM, ctx(_run(action, final_text)), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert ECHO_REASON in verdict.reason


@pytest.mark.parametrize("case", GENUINE)
async def test_page_text_beyond_agent_input_is_evidence(case: str) -> None:
    action, final_text = GENUINE[case]
    verdict = await judge_claim(CLAIM, ctx(_run(action, final_text)), default_registry())
    assert verdict.status is Support.SUPPORTED


def _detour_run(first: str, network_extra: list[NetworkEvent]) -> Run:
    """Opens `first`, then the shop, then clicks through to a page that shows CLAIM's text."""
    return make_run(
        actions=[
            act(1, "goto", url=first, value=first),
            act(2, "goto", url=f"{HOST}/", value=f"{HOST}/"),
            act(3, "click", "#orders"),
        ],
        network=[net(2, "GET", "/"), *network_extra, net(4, "GET", "/orders/A77")],
        snapshots=[snap(2, "/", "Welcome")],
        final_state=final("/orders/A77", "Order A77 confirmed"),
    )


async def test_url_typed_for_another_site_is_not_echoed_by_this_one() -> None:
    run = _detour_run("https://evil.test/Order A77 confirmed?note=Order A77 confirmed", [])
    verdict = await judge_claim(CLAIM, ctx(run), default_registry())
    assert verdict.status is Support.SUPPORTED, verdict.reason


async def test_url_input_relayed_to_this_site_is_still_an_echo() -> None:
    relayed = net(3, "GET", "/search?q=Order+A77+confirmed")
    run = _detour_run("https://evil.test/?note=Order A77 confirmed", [relayed])
    verdict = await judge_claim(CLAIM, ctx(run), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert ECHO_REASON in verdict.reason


async def test_url_typed_for_this_site_is_still_an_echo() -> None:
    run = _detour_run(f"{HOST}/Order A77 confirmed", [])
    verdict = await judge_claim(CLAIM, ctx(run), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert ECHO_REASON in verdict.reason


SHORT_SPEC = TaskSpec.model_validate(
    {
        "id": "buy",
        "task": "Buy a red kettle under $30",
        "site": "shop.test",
        "expect": [
            {
                "id": "order",
                "all": [{"kind": "request", "method": "POST", "url_contains": ["/api/orders"]}],
            },
            {"id": "conf", "any": [{"kind": "page_text", "contains": "Thank you for your order"}]},
        ],
    }
)


async def test_a_short_word_the_agent_typed_does_not_mask_the_sites_sentence() -> None:
    run = make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/"),
            act(2, "fill", "#name", value="Your"),
            act(3, "click", "#place", url=f"{HOST}/checkout"),
        ],
        network=[
            net(1, "GET", "/"),
            net(4, "POST", "/api/orders", 201, body={"items": [{"price": 24}]}),
            net(5, "GET", "/order/A1"),
        ],
        snapshots=[snap(1, "/", "Kettles Red Kettle $24.00")],
        final_state=final("/order/A1", "Thank you for your order! Red Kettle $24.00"),
    ).model_copy(update={"agent_answer": "I bought the Red Kettle. 'Thank you for your order'."})
    verdict = await JUDGE.judge(run, JudgeOptions(offline=True, spec=SHORT_SPEC))
    assert verdict.outcome is Outcome.PASS, verdict.summary
    assert [e.met for e in verdict.expectations] == [True, True]


def _short_echo(typed: str, text: str) -> Run:
    return make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), act(2, "fill", "#q", value=typed)],
        network=[net(1, "GET", "/"), net(3, "GET", "/search")],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=final("/search", text),
    )


@pytest.mark.parametrize(
    ("predicate", "typed", "text"),
    [
        (PageTextPredicate(contains="Paid"), "Paid", "Results for Paid"),
        (PageTextPredicate(contains="paid!"), "Paid", "Results for PAID!"),
        (
            ValuePredicate(field="status", page_regex=r"for (\w+)", op="==", value="Paid"),
            "Paid",
            "Results for Paid",
        ),
    ],
    ids=["word", "word-and-punctuation", "page-value"],
)
async def test_a_short_word_still_cannot_prove_itself(
    predicate: Predicate, typed: str, text: str
) -> None:
    res = await guarded(predicate, ctx(_short_echo(typed, text)), default_registry(), "it")
    assert res.status is Support.NO_EVIDENCE
    assert ECHO_REASON in res.reason


@pytest.mark.parametrize(
    "predicate",
    [UrlPredicate(contains="/cart"), UrlPredicate(regex=r"/cart$"), UrlPredicate(path="/cart")],
)
async def test_a_short_url_path_the_agent_typed_cannot_prove_itself(predicate: Predicate) -> None:
    run = make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), act(2, "goto", url=f"{HOST}/cart")],
        network=[net(1, "GET", "/"), net(3, "GET", "/cart")],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=final("/cart", "Your cart is empty"),
    )
    claim = CLAIM.model_copy(update={"predicates": [predicate]})
    verdict = await judge_claim(claim, ctx(run), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert ECHO_REASON in verdict.reason


def test_short_words_are_inputs_but_only_the_short_word_mask_covers_them() -> None:
    index = EvidenceIndex(_short_echo("Your", "Thank you for your order"))
    assert index.agent_inputs() == ("your",)
    assert index.without_agent_input is index
    final_state = index.without_short_word_input.final
    assert final_state is not None
    assert final_state.text == f"thank you for {MASK} order"


@pytest.mark.parametrize(
    ("text", "short"),
    [
        ("your", True),
        ("Paid!", True),
        ("/cart", True),
        ("kettle", False),
        ("a77", False),
        ("red kettle", False),
        ("in-stock", False),
    ],
)
def test_short_word_rule(text: str, short: bool) -> None:
    assert is_short_word(text) is short


STOCK = {
    "id": "stock",
    "task": "Is the red kettle in stock?",
    "site": "shop.test",
    "expect": [
        {"id": "in_stock", "all": [{"kind": "page_text", "contains": "In stock", "scope": "final"}]}
    ],
}
STOCK_SPEC = TaskSpec.model_validate(STOCK)
SEARCH_SPEC = TaskSpec.model_validate(
    {**STOCK, "allow": [{"kind": "request", "method": "POST", "url_contains": ["/api/search"]}]}
)
PRODUCT_PAGES = [snap(1, "/", "Kettles Red Kettle Blue Kettle"), snap(2, "/p/red", "Out of stock")]
FILTER_ECHO = "Red Kettle Out of stock. No variants match In stock"


def _edited(*edits: tuple[str, str]) -> list[ActionEvent]:
    return [act(3 + i, name, "#q", value=value) for i, (name, value) in enumerate(edits)]


async def _judged(run: Run, spec: TaskSpec) -> Outcome:
    run = run.model_copy(update={"agent_answer": "Yes, In stock."})
    return (await JUDGE.judge(run, JudgeOptions(offline=True, spec=spec))).outcome


@pytest.mark.parametrize(
    "edits",
    [
        [("fill", "In stocX"), ("press", "Backspace"), ("press", "k")],
        [("fill", "XIn stock"), ("send_keys", "Home Delete")],
        [("fill", "In stocX"), ("press", "Shift+ArrowLeft"), ("type", "k")],
        [("fill", "Out of stock"), ("press", "Control+a"), ("type", "In stock")],
    ],
    ids=["backspace", "home-delete", "selection", "select-all"],
)
async def test_an_edited_filter_echoing_the_needle_goes_to_review(
    edits: list[tuple[str, str]],
) -> None:
    run = make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), act(2, "click", "#red"), *_edited(*edits)],
        network=[net(1, "GET", "/"), net(2, "GET", "/p/red")],
        snapshots=PRODUCT_PAGES,
        final_state=final("/p/red", FILTER_ECHO),
    )
    assert await _judged(run, STOCK_SPEC) is Outcome.NEEDS_REVIEW


async def test_an_edited_search_echoed_by_an_allowed_search_api_goes_to_review() -> None:
    edits = [("fill", "In stocX"), ("press", "Backspace"), ("press", "k"), ("press", "Enter")]
    run = make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), act(2, "click", "#red"), *_edited(*edits)],
        network=[
            net(1, "GET", "/"),
            net(2, "GET", "/p/red"),
            net(7, "POST", "/api/search", body={"q": "In stock"}, response={"query": "In stock"}),
        ],
        snapshots=PRODUCT_PAGES,
        final_state=final("/search", "Search results for In stock: no products"),
    )
    assert await _judged(run, SEARCH_SPEC) is Outcome.NEEDS_REVIEW


def _pasted(copied_page: str) -> Run:
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/"),
            act(2, "click", "#blue"),
            act(3, "press", "#stock", value="Control+a"),
            act(4, "press", value="Control+c"),
            act(5, "click", "#red-link"),
            act(6, "click", "#filter"),
            act(7, "press", "#filter", value="Control+v"),
        ],
        network=[net(1, "GET", "/"), net(2, "GET", "/p/blue"), net(5, "GET", "/p/red")],
        snapshots=[
            snap(1, "/", "Kettles Red Kettle Blue Kettle"),
            snap(2, "/p/blue", copied_page),
            snap(5, "/p/red", "Red Kettle"),
        ],
        final_state=final("/p/red", "Red Kettle In stock. No variants match Blue Kettle"),
    )


async def test_text_pasted_from_another_page_cannot_prove_the_needle() -> None:
    assert await _judged(_pasted("Blue Kettle In stock"), STOCK_SPEC) is Outcome.NEEDS_REVIEW


async def test_a_paste_of_text_without_the_needle_leaves_the_page_as_evidence() -> None:
    assert await _judged(_pasted("Blue Kettle"), STOCK_SPEC) is Outcome.PASS


async def test_a_corrected_typo_does_not_mask_a_confirmation_proven_by_other_evidence() -> None:
    edits = [("fill", "red kettel"), ("press", "Backspace"), ("send_keys", "Backspace l e")]
    run = make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/"),
            *_edited(*edits),
            act(10, "click", "#place", url=f"{HOST}/checkout"),
        ],
        network=[
            net(1, "GET", "/"),
            net(11, "POST", "/api/orders", 201, body={"items": [{"price": 24}]}),
            net(12, "GET", "/order/A1"),
        ],
        snapshots=[snap(1, "/", "Kettles Red Kettle $24.00")],
        final_state=final("/order/A1", "Thank you for your order! Red Kettle $24.00"),
    ).model_copy(update={"agent_answer": "I bought the Red Kettle. 'Thank you for your order'."})
    verdict = await JUDGE.judge(run, JudgeOptions(offline=True, spec=SHORT_SPEC))
    assert verdict.outcome is Outcome.PASS, verdict.summary
    assert EvidenceIndex(run).agent_inputs()[:2] == ("red kettel", "red kettle")


def _fields(*values: str, text: str, response: object | None = None) -> Run:
    """Fills one field per value on the product page, which then shows `text`."""
    fills = [act(3 + i, "fill", f"#f{i}", value=v) for i, v in enumerate(values)]
    return make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), act(2, "click", "#red"), *fills],
        network=[
            net(1, "GET", "/"),
            net(2, "GET", "/p/red"),
            net(9, "GET", "/api/preview", response=response),
        ],
        snapshots=PRODUCT_PAGES,
        final_state=final("/p/red", text),
    )


@pytest.mark.parametrize(
    ("values", "text"),
    [
        (("In", "stock"), "Red Kettle Out of stock. Engraving: In stock"),
        (("in", "STOCK!"), "Red Kettle Out of stock. Engraving: In stock"),
        (("stock", "In"), "Red Kettle Out of stock. Engraving: In stock"),
        (("I", "n stock"), "Red Kettle Out of stock. Engraving: In stock"),
        (("I", "n", "st", "ock"), "Red Kettle Out of stock. Engraving: In stock"),
    ],
    ids=["two-fields", "folded", "reversed", "split-word", "letters"],
)
async def test_a_needle_composed_from_short_inputs_goes_to_review(
    values: tuple[str, ...], text: str
) -> None:
    assert await _judged(_fields(*values, text=text), STOCK_SPEC) is Outcome.NEEDS_REVIEW


@pytest.mark.parametrize(
    ("values", "text"),
    [
        (("In",), "Red Kettle In stock. Engraving: In"),
        (("tin", "stock"), "Red Kettle In stock. Engraving: tin stock"),
        (("Red Kettle", "1"), "Red Kettle In stock"),
    ],
    ids=["one-word-typed", "inside-a-word", "unrelated"],
)
async def test_a_needle_with_a_word_the_agent_did_not_type_is_evidence(
    values: tuple[str, ...], text: str
) -> None:
    assert await _judged(_fields(*values, text=text), STOCK_SPEC) is Outcome.PASS


async def test_short_inputs_do_not_cover_the_sites_sentence() -> None:
    run = _fields("Your", "order", "A1", text="Thank you for your order")
    res = await guarded(
        PageTextPredicate(contains="Thank you for your order"), ctx(run), default_registry(), "it"
    )
    assert res.status is Support.SUPPORTED, res.reason


async def test_a_response_composed_from_short_inputs_proves_nothing() -> None:
    run = _fields("In", "stock", text="Red Kettle", response={"preview": "In stock"})
    predicate = RequestPredicate(url_contains=["/api/preview"], response_contains=["In stock"])
    res = await guarded(predicate, ctx(run), default_registry(), "it")
    assert res.status is Support.NO_EVIDENCE
    assert ECHO_REASON in res.reason


def test_the_covered_mask_masks_only_covered_occurrences() -> None:
    index = EvidenceIndex(_fields("In", "stock", text="In stock now"))
    assert index.without_covered(()) is index
    masked = index.without_covered(("In stock",)).final
    assert masked is not None
    assert masked.text == f"{MASK} now"
    partial = EvidenceIndex(_fields("In", text="In stock now")).without_covered(("In stock",))
    assert partial.final is not None
    assert partial.final.text == "In stock now"
