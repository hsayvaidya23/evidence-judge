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


def test_masking_keeps_evidence_without_agent_input() -> None:
    index = EvidenceIndex(make_run(network=[net(1, "GET", "/")], final_state=final("/", "Hi")))
    assert index.without_agent_input is index
    run = honest_run()
    masked = EvidenceIndex(run).without_agent_input
    assert masked.final == run.final_state
