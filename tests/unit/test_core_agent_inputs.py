"""What the agent put into a run itself, and the evidence with it masked out."""

from __future__ import annotations

from evidence_judge.core.echo import MASK
from evidence_judge.core.evidence import ActionEvent, NetworkEvent
from evidence_judge.core.index import EvidenceIndex
from tests.unit.conftest import HOST, act, final, honest_run, make_run, net, snap


def _inputs(
    actions: list[ActionEvent], network: list[NetworkEvent] | None = None
) -> tuple[str, ...]:
    return EvidenceIndex(make_run(actions=actions, network=network)).agent_inputs()


def _clicks(*labels: str | None, start: int = 1) -> list[ActionEvent]:
    return [
        act(start + i, "click", f"#k{i}").model_copy(update={"target_text": label})
        for i, label in enumerate(labels)
    ]


def test_clicks_in_a_row_on_short_label_controls_form_one_input() -> None:
    assert _inputs(_clicks("I", "n", " ", "s", "t", "o", "c", "k")) == ("in stock",)
    assert _inputs(_clicks("In", "stock")) == ("instock",)
    assert _inputs(_clicks("In", "Space", "stock ")) == ("in stock",)
    assert _inputs(_clicks("I", "n", "Add to cart", "o", "k")) == ()
    split = [*_clicks("I", "n"), act(3, "press", "#q", value="Enter"), *_clicks("s", start=4)]
    assert _inputs(split) == ()


def test_a_clicked_label_falls_back_to_the_text_its_selector_names() -> None:
    clicks = [
        act(1, "click", "text=I"),
        act(2, "click", 'button:has-text("n")'),
        act(3, "click", 'internal:role=button[name=" "i]'),
        act(4, "click", "#pad >> text='st' >> nth=0"),
        act(5, "click", 'internal:text="ock"i'),
    ]
    assert _inputs(clicks) == ("in stock",)
    assert _inputs([act(1, "click", "#a"), act(2, "click", "xpath=//b")]) == ()


def test_a_single_click_or_long_labels_are_not_input() -> None:
    assert _inputs(_clicks("Buy")) == ()
    assert _inputs(_clicks("Red Kettle", "Add to cart", "Checkout", None)) == ()


def test_typing_actions_are_inputs_under_any_alias() -> None:
    actions = [
        act(1, "fill", "#q", value="Order  A77"),
        act(2, "type", "#r", value="Refund"),
        act(3, "insert_text", "#t", value="Total: $500"),
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
    assert _inputs(actions=actions) == ("a77 okxyz", "a77 ok", "abc", "xyz")


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
    assert inputs == (
        "order a77 confirmed",
        "payment=success",
        "receipt",
        "orders",
        "a 77",
        "note",
    )


def test_reopening_a_url_the_site_sent_the_browser_to_is_not_input() -> None:
    redirect = net(2, "POST", "/api/orders", 303).model_copy(
        update={"response_headers": {"Location": "/order/Shipped-A77"}}
    )
    actions = [
        act(1, "click", "#buy"),
        act(3, "click", "#details"),
        act(4, "goto", url=f"{HOST}/order/Shipped-A77"),
        act(5, "goto", url=f"{HOST}/track/Linked-B12"),
    ]
    network = [redirect, net(3, "GET", "/track/Linked-B12")]
    assert _inputs(actions=actions, network=network) == ()


def test_the_fragment_of_a_url_the_agent_opens_is_input_even_on_a_served_page() -> None:
    actions = [
        act(1, "goto", url=f"{HOST}/p/red"),
        act(2, "click", "h1"),
        act(3, "goto", url=f"{HOST}/p/red#In%20stock"),
        act(4, "goto", url=f"{HOST}/p/red#/tab=Shipped-A77&sort"),
    ]
    network = [net(1, "GET", "/p/red")]
    assert _inputs(actions=actions, network=network) == (
        "/tab=shipped-a77&sort",
        "shipped-a77",
        "in stock",
        "sort",
        "red",
        "tab",
    )


def test_query_keys_without_a_value_are_input() -> None:
    actions = [act(1, "goto", url=f"{HOST}/p/red?In%20stock&Sold+out=")]
    network = [net(2, "GET", "/search?Order%20A77")]
    assert _inputs(actions=actions, network=network) == (
        "order a77",
        "in stock",
        "sold out",
        "red",
    )


def test_a_url_the_agent_opened_first_stays_input_when_the_site_serves_it_later() -> None:
    actions = [act(1, "goto", url=f"{HOST}/search/Order-A77-confirmed")]
    network = [
        net(2, "GET", "/search/Order-A77-confirmed"),
        net(3, "GET", "/search/Order-A77-confirmed"),
    ]
    assert _inputs(actions=actions, network=network) == ("order-a77-confirmed", "search")


def test_a_load_that_answers_the_goto_itself_does_not_make_its_url_site_served() -> None:
    actions = [act(1, "click", "#menu"), act(3, "goto", url=f"{HOST}/orders/A77-confirmed")]
    network = [net(2, "GET", "/orders/A77-confirmed")]
    assert _inputs(actions=actions, network=network) == ("a77-confirmed", "orders")


def test_pressed_text_is_input_but_key_names_and_chords_are_not() -> None:
    keys = ["Enter", "Control+A", "Shift+Tab", "Control+Shift+T", "ArrowDown ArrowDown Enter"]
    actions = [
        *[act(1 + i, "press", "#q", value=key) for i, key in enumerate(keys)],
        act(10, "press", "#q", value="Control++"),
        act(11, "press", "#q", value="F12"),
        act(12, "send_keys", "#q", value="Order A77 confirmed"),
        act(13, "press", "#q", value="London"),
    ]
    assert _inputs(actions=actions) == (
        "order a77 confirmedlondon",
        "order a77 confirmed",
        "london",
    )


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


def test_chosen_options_are_inputs_under_any_alias() -> None:
    actions = [
        act(1, "selectOption", "#avail", value="In stock"),
        act(2, "select_dropdown_option", value="Express shipping"),
        act(3, "select", "#sizes", value="Large tray, Small tray"),
    ]
    assert _inputs(actions=actions) == (
        "large tray, small tray",
        "express shipping",
        "large tray",
        "small tray",
        "in stock",
    )


def test_a_page_repeating_the_chosen_option_is_masked() -> None:
    run = make_run(
        actions=[act(1, "selectOption", "#avail", value="In stock")],
        final_state=final("/sizes", "Out of stock Showing: In stock"),
    )
    masked = EvidenceIndex(run).without_agent_input.final
    assert masked is not None
    assert masked.text == f"out of stock showing: {MASK}"


def test_values_fields_held_in_snapshots_are_inputs() -> None:
    page = snap(2, "/reviews", "Out of stock No reviews mention In stock")
    run = make_run(
        actions=[act(1, "click", "#q")],
        snapshots=[page.model_copy(update={"field_values": ("In stock", "1")})],
        final_state=final("/reviews", "Out of stock No reviews mention In stock"),
    )
    index = EvidenceIndex(run)
    assert index.agent_inputs() == ("in stock",)
    masked = index.without_agent_input.final
    assert masked is not None
    assert masked.text == f"out of stock no reviews mention {MASK}"


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


def test_masking_covers_echoes_that_differ_in_case_spaces_and_punctuation() -> None:
    text = "No results for instock. Did you mean: In stock?"
    assert _masked_final(["instock"], text) == f"no results for {MASK}. did you mean: {MASK}?"
    assert _masked_final(["In-stock"], "Showing: In stock") == f"showing: {MASK}"
    assert _masked_final(["order-1042 confirmed"], "Order #1042 confirmed today") == (
        f"{MASK} today"
    )
    assert _masked_final(["Order 1042 confirmedXYZ"], "Order #1042-confirmed") == MASK


def test_folded_echoes_start_and_end_on_word_boundaries() -> None:
    assert _masked_final(["nstock"], "In stock") == "In stock"
    assert _masked_final(["tin"], "Got in stock") == "Got in stock"
    assert _masked_final(["zz redkettles"], "Red kettle") == "Red kettle"
    assert _masked_final(["zz red kettle"], "Red-kettle and Redkettles") == (
        f"{MASK} and redkettles"
    )


def test_urls_are_not_masked_for_folded_echoes() -> None:
    run = make_run(
        actions=[act(1, "fill", "#q", value="Red Kettle")],
        network=[net(2, "GET", "/products/red-kettle")],
        final_state=final("/products/red-kettle", "Red-Kettle"),
    )
    masked = EvidenceIndex(run).without_agent_input
    assert masked.final is not None
    assert masked.final.url.endswith("/products/red-kettle")
    assert masked.final.text == MASK
    assert masked.run.network[0].url.endswith("/products/red-kettle")


def test_masking_keeps_evidence_without_agent_input() -> None:
    index = EvidenceIndex(make_run(network=[net(1, "GET", "/")], final_state=final("/", "Hi")))
    assert index.without_agent_input is index
    run = honest_run()
    masked = EvidenceIndex(run).without_agent_input
    assert masked.final == run.final_state


def _keys(*steps: tuple[str, str]) -> list[ActionEvent]:
    return [act(1 + i, name, "#q", value=value) for i, (name, value) in enumerate(steps)]


def test_edits_are_replayed_and_the_edited_value_is_an_input() -> None:
    typo = _keys(("fill", "In stocX"), ("press", "Backspace"), ("press", "k"))
    assert _inputs(actions=typo) == ("in stock", "in stocx")


def test_edits_sent_as_one_key_sequence_are_replayed() -> None:
    sequence = _keys(("fill", "XXIn stock"), ("send_keys", "Home Delete Delete"))
    assert _inputs(actions=sequence) == ("xxin stock", "xin stock", "in stock")


def test_cursor_moves_and_selections_are_replayed() -> None:
    insert = _keys(("fill", "Order confirmed"), ("press", "Control+ArrowLeft"))
    insert += [act(10 + i, "press", "#q", value=c) for i, c in enumerate("A77 ")]
    assert "order a77 confirmed" in _inputs(actions=insert)
    shift = _keys(("fill", "Order pending"), ("press", "Shift+Home"), ("type", "Paid"))
    assert "paid" in _inputs(actions=shift)
    words = _keys(("fill", "Paid in full not"), ("press", "Alt+Backspace"))
    assert "paid in full" in _inputs(actions=words)
    line = _keys(("fill", "abc"), ("press", "Meta+Backspace"), ("press", "KeyX Digit7 Space KeyZ"))
    assert "x7 z" in _inputs(actions=line)


def test_select_all_replaces_and_undo_starts_the_field_over() -> None:
    replaced = _keys(("fill", "kettle"), ("press", "Control+a"), ("type", "In stock"))
    assert _inputs(actions=replaced) == ("in stock", "kettle")
    undone = _keys(("fill", "Order A77"), ("press", "Control+z"), ("type", "Paid now"))
    assert _inputs(actions=undone) == ("order a77", "paid now")


def test_text_cut_or_copied_from_a_field_is_pasted_into_the_value() -> None:
    actions = [
        *_keys(("fill", "In stock"), ("press", "Meta+A"), ("press", "Meta+X")),
        act(10, "fill", "#other", value="Status: "),
        act(11, "press", "#other", value="Meta+V"),
        act(12, "press", "#other", value="Shift+ArrowLeft Shift+ArrowLeft Control+c"),
        act(13, "press", "#other", value="End Control+v"),
    ]
    assert _inputs(actions=actions) == ("status: in stockck", "in stock", "status:")


def test_text_copied_from_the_page_makes_each_of_its_lines_and_sentences_an_input() -> None:
    run = make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/p/blue"),
            act(2, "press", "#stock", value="Control+a"),
            act(3, "press", value="Control+c"),
            act(4, "click", "#filter"),
            act(5, "press", value="Control+v"),
        ],
        snapshots=[snap(1, "/p/blue", "Blue Kettle\nIn stock. Ships today!")],
    )
    inputs = EvidenceIndex(run).agent_inputs()
    assert inputs == ("ships today!", "blue kettle", "in stock.", "blue")


def test_a_copy_reads_the_page_on_screen_and_a_paste_without_a_copy_reads_every_page() -> None:
    copy_then_snapshot = make_run(
        actions=[act(1, "click", "#a"), act(2, "press", value="Control+c")],
        snapshots=[snap(2, "/", "Later page")],
    )
    assert EvidenceIndex(copy_then_snapshot).agent_inputs() == ("later page",)
    last_copy = make_run(
        actions=[act(1, "press", value="Meta+c")], final_state=final("/", "Final page")
    )
    assert EvidenceIndex(last_copy).agent_inputs() == ("final page",)
    paste = make_run(
        actions=[act(3, "press", "#q", value="Control+v"), act(4, "press", "#q", value="Paid")],
        snapshots=[snap(1, "/", "First page"), snap(2, "/b", "Second page")],
    )
    assert EvidenceIndex(paste).agent_inputs() == ("second page", "first page", "paid")


def test_masking_covers_parts_of_an_input_from_any_word_start() -> None:
    assert _masked_final(["zz In stock"], "Showing In stock items") == f"showing {MASK} items"
    assert _masked_final(["Kettle In stock now"], "Kettle In stock") == MASK
    assert _masked_final(["#A77 confirmed today"], "Order A77 confirmed") == (f"order {MASK}")


def test_parts_never_start_inside_a_word_and_later_parts_end_at_a_word_end() -> None:
    assert _masked_final(["unconfirmed booking"], "Booking confirmed") == "Booking confirmed"
    assert _masked_final(["#A77 confirmation"], "Order A77 confirmed") == ("Order A77 confirmed")
    assert _masked_final(["zz Order A77 confirmation"], "Order A77 confirmed") == (
        f"{MASK}confirmed"
    )


def test_parts_never_start_inside_a_redaction_token() -> None:
    card = "[REDACTED:card:8be88632]"
    text = "Card [REDACTED:card:96a7866f] on file"
    assert _masked_final([f"Use {card} today"], text) == text


def test_word_and_line_moves_are_replayed() -> None:
    words = _keys(
        ("fill", "Order A77 -- "),
        ("press", "Control+Backspace"),
        ("press", "Meta+ArrowLeft"),
        ("press", "Control+Delete"),
        ("type", "Paid"),
    )
    assert _inputs(actions=words) == ("order a77 --", "order", "paid")
    ends = _keys(("fill", " -- tail"), ("press", "Home Control+Delete"), ("type", "Paid"))
    assert "paid" in _inputs(actions=ends)


def test_many_inputs_are_matched_in_one_pass_over_the_text() -> None:
    typed = [f"Product {i:03d} in stock" for i in range(80)]
    text = "Catalogue: product 007 in stock, item 999 sold out"
    assert _masked_final(typed, text) == f"catalogue: {MASK}, item 999 sold out"


def test_keys_held_down_type_and_their_release_does_nothing() -> None:
    held = _keys(*[(name, c) for c in "In stock" for name in ("key_down", "key_up")])
    held += _keys(("key_down", "Enter"), ("key_up", "Enter"))
    assert "in stock" in _inputs(actions=held)


def test_a_held_modifier_applies_to_the_keys_pressed_until_its_release() -> None:
    replaced = _keys(
        ("fill", "kettle"),
        ("key_down", "Control"),
        ("key_down", "a"),
        ("key_up", "a"),
        ("key_up", "Control"),
        ("type", "In stock"),
    )
    assert _inputs(actions=replaced) == ("in stock", "kettle")
    shifted = _keys(
        ("key_down", "Shift"),
        ("press", "I"),
        ("key_up", "Shift"),
        ("key_down", "n"),
        ("key_down", " "),
        ("key_down", "s"),
    )
    assert "in s" in _inputs(actions=shifted)
    released = _keys(("fill", "kettle"), ("key_down", "Meta"), ("key_up", "Meta"), ("press", "a"))
    assert "kettlea" in _inputs(actions=released)
