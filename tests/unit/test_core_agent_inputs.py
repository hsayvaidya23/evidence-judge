"""What the agent put into a run itself, and the evidence with it masked out."""

from __future__ import annotations

from evidence_judge.core.evidence import ActionEvent, NetworkEvent
from evidence_judge.core.index import MASK, EvidenceIndex
from tests.unit.conftest import HOST, act, final, honest_run, make_run, net, snap


def _inputs(
    actions: list[ActionEvent], network: list[NetworkEvent] | None = None
) -> tuple[str, ...]:
    return EvidenceIndex(make_run(actions=actions, network=network)).agent_inputs()


def test_typing_actions_are_inputs_under_any_alias() -> None:
    actions = [
        act(1, "fill", "#q", value="Order  A77"),
        act(2, "type", "#q", value="Refund"),
        act(3, "insert_text", value="Total: $500"),
        act(4, "input", "#name", value="Ada"),
        act(5, "click", "#go", value="Ignored click value"),
    ]
    assert _inputs(actions=actions) == ("total: $500", "order a77", "refund", "ada")


def test_single_key_presses_on_one_target_form_one_input() -> None:
    keys = [act(1 + i, "press", "#q", value=c) for i, c in enumerate("A77 ok")]
    actions = [
        *keys,
        act(20, "press", "#q", value="Enter"),
        *[act(21 + i, "press", "#q", value=c) for i, c in enumerate("xyz")],
        *[act(30 + i, "press", "#other", value=c) for i, c in enumerate("abc")],
    ]
    assert _inputs(actions=actions) == ("a77 ok", "abc", "xyz")


def test_goto_paths_and_queries_and_get_queries_are_inputs() -> None:
    inputs = _inputs(
        actions=[
            act(1, "goto", value=f"{HOST}/orders/A%2077?note=Order+A77+confirmed&n=1"),
            act(2, "navigate", url=f"{HOST}/receipt"),
        ],
        network=[
            net(1, "GET", "/search?q=payment%3Dsuccess"),
            net(2, "POST", "/api/orders?coupon=SAVE10"),
            net(3, "GET", "/api/items"),
        ],
    )
    assert inputs == ("order a77 confirmed", "payment=success", "receipt", "orders", "a 77")


def test_pressed_text_is_input_but_key_names_and_chords_are_not() -> None:
    keys = ["Enter", "Control+A", "Shift+Tab", "Control+Shift+T", "ArrowDown ArrowDown Enter"]
    actions = [
        *[act(1 + i, "press", "#q", value=key) for i, key in enumerate(keys)],
        act(10, "press", "#q", value="Control++"),
        act(11, "press", "#q", value="F12"),
        act(12, "send_keys", "#q", value="Order A77 confirmed"),
        act(13, "press", "#q", value="London"),
    ]
    assert _inputs(actions=actions) == ("order a77 confirmed", "london")


def test_typed_markup_is_also_input_as_rendered_text() -> None:
    actions = [
        act(1, "fill", "#bio", value="<b>Order A77</b> confirmed"),
        act(2, "fill", "#note", value="Tom &amp; Jerry"),
        act(3, "fill", "#plain", value="a < b > c"),
    ]
    assert _inputs(actions=actions) == (
        "<b>order a77</b> confirmed",
        "order a77 confirmed",
        "tom &amp; jerry",
        "tom & jerry",
        "a < b > c",
    )


def test_uploaded_file_names_with_and_without_extension_are_inputs() -> None:
    actions = [
        act(1, "upload", "#a", value=r"C:\fakepath\Order A77.pdf"),
        act(2, "setInputFiles", "#b", value="scan one.png, notes"),
    ]
    assert _inputs(actions=actions) == (
        "scan one.png, notes",
        "order a77.pdf",
        "scan one.png",
        "order a77",
        "scan one",
        "notes",
    )


def test_short_or_symbol_only_inputs_are_ignored() -> None:
    assert _inputs(actions=[act(1, "fill", value="ab"), act(2, "fill", value="--- ")]) == ()


def test_masking_replaces_every_occurrence_in_text_and_urls() -> None:
    run = make_run(
        actions=[act(1, "fill", "#q", value="A77 confirmed")],
        network=[net(2, "GET", "/search?q=A77+confirmed")],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=final("/search?q=A77+confirmed", "No results for Order A77 Confirmed"),
    )
    masked = EvidenceIndex(run).without_agent_input
    assert masked.final is not None
    assert masked.final.text == f"no results for order {MASK}"
    assert masked.final.url == f"{HOST}/search?q=%00"
    assert masked.run.network[0].url == f"{HOST}/search?q=%00"
    assert masked.run.snapshots[0].text == "Welcome"
    assert masked.run.snapshots[0].url == f"{HOST}/"


def _masked_final(typed: list[str], text: str) -> str:
    actions = [act(1 + i, "fill", f"#f{i}", value=value) for i, value in enumerate(typed)]
    masked = EvidenceIndex(make_run(actions=actions, final_state=final("/", text)))
    state = masked.without_agent_input.final
    assert state is not None
    return state.text


def test_masking_covers_echoes_cut_short_of_the_input() -> None:
    text = "Hello Order A77 confirmed. Again: Order A77 c and Order A"
    assert _masked_final(["Order A77 confirmed!!!!!!"], text) == (
        f"hello {MASK}. again: {MASK} and order a"
    )


def test_masking_ignores_shared_prefixes_shorter_than_min_echo_prefix() -> None:
    text = "Your order is confirmed"
    assert _masked_final(["confirmation code"], text) == text
    assert _masked_final(["confirmXYZ"], text) == text
    assert _masked_final(["confirmeX"], text) == f"your order is {MASK}d"
    assert _masked_final(["confirmed by phone"], text) == f"your order is {MASK}"


def test_cut_short_echoes_never_end_inside_a_redaction_token() -> None:
    card = "[REDACTED:card:8be88632]"
    text = f"Card [REDACTED:card:96a7866f] for [REDACTED:email:68cc336b] Ship to {card} Main St"
    assert _masked_final([f"{card} Main Street"], text) == (
        f"card [redacted:card:96a7866f] for [redacted:email:68cc336b] ship to {MASK}"
    )


def test_masking_merges_overlapping_echoes() -> None:
    text = "Ship to 12 Long Street London today"
    assert _masked_final(["12 Long Street", "Long Street London"], text) == (
        f"ship to {MASK} today"
    )


def test_masking_extends_each_occurrence_on_repetitive_text() -> None:
    text = "ab" * 6 + "x " + "ab" * 3
    assert _masked_final(["abababababab"], text) == f"{MASK}x ababab"


def test_masking_keeps_evidence_without_agent_input() -> None:
    index = EvidenceIndex(make_run(network=[net(1, "GET", "/")], final_state=final("/", "Hi")))
    assert index.without_agent_input is index
    run = honest_run()
    masked = EvidenceIndex(run).without_agent_input
    assert masked.final == run.final_state
