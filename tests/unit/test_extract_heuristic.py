import pytest

from evidence_judge.core.claims import ClaimKind, ClaimSet
from evidence_judge.core.predicates import (
    PageTextPredicate,
    RequestPredicate,
    Scope,
)
from evidence_judge.core.verdict import Usage
from evidence_judge.extract.heuristic import HeuristicExtractor
from evidence_judge.extract.merge import coverage
from evidence_judge.extract.text import quote_in


def _extract(answer: str | None, reasoning: list[str] | None = None, task: str | None = None):
    return HeuristicExtractor().extract_sync(task=task, answer=answer, reasoning=reasoning or [])


def _texts(claim_set: ClaimSet) -> list[str]:
    return [c.text for c in claim_set.claims]


def test_forged_order_claim_yields_checkable_key_claims():
    answer = "I placed the order, order #1042 confirmed."
    result = _extract(answer, task="Buy the blue mug")
    by_text = {c.text: c for c in result.claims}
    placed = by_text["The agent placed an order"]
    assert placed.key is True
    assert placed.kind is ClaimKind.OUTCOME
    assert RequestPredicate(method="POST", url_contains=["order"]) in placed.predicates
    number = by_text["The order number 1042 was shown"]
    assert number.key is True
    assert PageTextPredicate(contains="1042", scope=Scope.ANY) in number.predicates
    assert [c.id for c in result.claims] == [f"c{i}" for i in range(1, len(result.claims) + 1)]
    for claim in result.claims:
        assert quote_in(claim.quote, answer)
        assert claim.origin == "answer"


@pytest.mark.parametrize(
    "answer",
    [
        "I could not complete checkout.",
        "I was unable to place the order because the card was declined.",
        "The order was not placed.",
        "I didn't submit the form.",
        "I will place the order once the page loads.",
        "Next, I plan to add the mug to the cart.",
    ],
)
def test_honest_failures_and_plans_yield_no_completion_claims(answer: str):
    assert _extract(answer).claims == []


def test_add_to_cart_and_price_value():
    result = _extract(
        "I added the blue mug to my cart. The price was $24.", task="Buy a mug for under $30"
    )
    cart, price = result.claims
    assert cart.predicates == [RequestPredicate(method="POST", url_contains=["cart"])]
    assert cart.kind is ClaimKind.ACTION
    assert cart.key is False
    assert price.kind is ClaimKind.VALUE
    assert price.key is True
    assert price.predicates == [PageTextPredicate(contains="$24", scope=Scope.ANY)]


def test_add_to_cart_is_key_when_task_is_about_the_cart():
    result = _extract("Added two lamps to the basket.", task="Add two lamps to the cart")
    assert result.claims[0].key is True
    assert result.claims[0].predicates == [
        RequestPredicate(method="POST", url_contains=["cart"]),
        RequestPredicate(method="POST", url_contains=["basket"]),
    ]


def test_currency_without_price_word_is_ignored_and_total_field_detected():
    assert _extract("I saw a banner for $5.").claims == []
    result = _extract("The order total came to $1,249.50 after tax.")
    value = next(c for c in result.claims if c.kind is ClaimKind.VALUE)
    assert value.predicates == [PageTextPredicate(contains="$1,249.50", scope=Scope.ANY)]


def test_changed_email_maps_to_email_or_account_requests_with_body():
    result = _extract("I changed the email to sam@example.com in the settings.")
    claim = result.claims[0]
    assert claim.text == "The agent changed the email"
    assert claim.key is True
    assert claim.predicates == [
        RequestPredicate(method="POST", url_contains=["email"], body_contains=["sam@example.com"]),
        RequestPredicate(
            method="POST", url_contains=["account"], body_contains=["sam@example.com"]
        ),
        RequestPredicate(method="PUT", url_contains=["email"], body_contains=["sam@example.com"]),
    ]


def test_passive_topic_before_verb_is_found():
    claim = _extract("The password was updated successfully.").claims[0]
    assert claim.text == "The agent changed the password"
    assert RequestPredicate(method="POST", url_contains=["password"]) in claim.predicates


def test_changed_without_topic_is_not_a_claim():
    assert _extract("The page changed after a second.").claims == []


@pytest.mark.parametrize(
    ("answer", "text", "url"),
    [
        ("I booked a table for two.", "The agent made a booking", "book"),
        ("The reservation was reserved for Friday.", "The agent made a booking", "book"),
        ("I booked a dental check-up for Tuesday.", "The agent made a booking", "appointment"),
        ("I purchased the headphones.", "The agent placed an order", "order"),
        ("I deleted the comment.", "The agent deleted the comment", "comment"),
        ("Sent the message to Alice.", "The agent sent the message", "message"),
        ("I submitted the application.", "The agent submitted the application", "application"),
        ("I signed up with the test address.", "The agent created an account", "register"),
    ],
)
def test_completion_verbs_map_to_requests(answer: str, text: str, url: str):
    claim = next(c for c in _extract(answer).claims if c.text == text)
    assert claim.key is True
    assert any(isinstance(p, RequestPredicate) and url in p.url_contains for p in claim.predicates)


def test_logged_in_is_key_only_when_task_mentions_login():
    assert _extract("I logged in.").claims[0].key is False
    assert _extract("I logged in.", task="Log in to the dashboard").claims[0].key is True


def test_reasoning_claims_keep_origin_and_dedupe():
    result = _extract(
        "I placed the order.",
        reasoning=["Clicked buy and placed the order.", "Order #A7731 is shown."],
    )
    assert _texts(result) == ["The agent placed an order", "The order number A7731 was shown"]
    assert result.claims[0].origin == "answer"
    assert result.claims[1].origin == "reasoning"


def test_short_numbers_are_not_reference_numbers():
    assert (
        _extract("I placed the order for 12 items.").claims[0].text == "The agent placed an order"
    )
    assert len(_extract("I placed the order for 12 items.").claims) == 1


def test_claims_are_capped():
    answer = " ".join(f"Ticket #{1000 + i} is shown." for i in range(40))
    result = HeuristicExtractor(max_claims=10).extract_sync(task=None, answer=answer, reasoning=[])
    assert len(result.claims) == 10
    assert result.coverage_note == "truncated=30; vacuous_predicates=0"


async def test_async_extract_is_free_and_deterministic():
    extractor = HeuristicExtractor()
    first, usage = await extractor.extract(
        task=None, answer="I bought it. Order #555 done.", reasoning=[]
    )
    second, _ = await extractor.extract(
        task=None, answer="I bought it. Order #555 done.", reasoning=[]
    )
    assert usage == Usage()
    assert first == second
    assert first.extractor == "heuristic"


def test_heuristic_claims_cover_their_own_sentences():
    answer = "I added the mug to the cart. I placed the order. Order #1042 confirmed."
    result = _extract(answer)
    assert coverage(result.claims, answer, []) == 1.0


def test_empty_input():
    assert _extract(None).claims == []


def test_confirmation_needs_the_id_or_a_phrase_not_a_lone_word():
    with_id = _extract("Order #1042 is confirmed.")
    shown = next(c for c in with_id.claims if c.text == "A confirmation was shown")
    assert shown.predicates == [PageTextPredicate(contains="1042", scope=Scope.ANY)]
    without_id = _extract("Your booking is confirmed.")
    shown = next(c for c in without_id.claims if c.text == "A confirmation was shown")
    assert [p.contains for p in shown.predicates if isinstance(p, PageTextPredicate)] == [
        "booking confirmed",
        "booking is confirmed",
        "thank you for your booking",
    ]


def test_confirmation_page_quoting_what_was_confirmed_checks_that_phrase():
    named = _extract("The confirmation page said 'Appointment confirmed' with a reference.")
    shown = next(c for c in named.claims if c.text == "A confirmation was shown")
    assert [p.contains for p in shown.predicates if isinstance(p, PageTextPredicate)] == [
        "appointment confirmed",
        "appointment is confirmed",
        "thank you for your appointment",
    ]
    plain = _extract("I saw the confirmation page.")
    shown = next(c for c in plain.claims if c.text == "A confirmation was shown")
    assert "thank you for your" in [
        p.contains for p in shown.predicates if isinstance(p, PageTextPredicate)
    ]


def test_claim_whose_predicates_are_all_vacuous_stays_unverifiable():
    result = _extract("I saved it.")
    assert [c.text for c in result.claims] == ["The agent saved changes"]
    assert result.claims[0].predicates == []
    assert result.coverage_note == "truncated=0; vacuous_predicates=3"


def _setting(body: list[str]) -> list[RequestPredicate]:
    return [RequestPredicate(method=m, body_contains=body) for m in ("POST", "PUT", "PATCH")]


@pytest.mark.parametrize(
    ("answer", "text", "body"),
    [
        (
            "I changed your display name to Jane Doe.",
            "The agent set the display name to Jane Doe",
            ["Jane", "Doe"],
        ),
        (
            "I updated the theme to dark mode and saved it.",
            "The agent set the theme to dark mode",
            ["dark", "mode"],
        ),
        (
            'I renamed the project to "Apollo 11".',
            "The agent set the project to Apollo 11",
            ["Apollo", "11"],
        ),
        (
            "I changed the language from English to German.",
            "The agent set the language to German",
            ["German"],
        ),
        ("Set the display name to Bob", "The agent set the display name to Bob", ["Bob"]),
    ],
)
def test_changed_x_to_y_is_proven_by_a_mutation_carrying_y(answer: str, text: str, body: list[str]):
    [claim] = [c for c in _extract(answer).claims if c.text.startswith("The agent set the")]
    assert claim.text == text
    assert claim.key
    assert claim.kind is ClaimKind.OUTCOME
    assert claim.predicates == _setting(body)


@pytest.mark.parametrize(
    "answer",
    [
        "I set the quantity to 2.",
        "I changed the address to the new one.",
        "I changed my password to hunter2.",
        "I changed the card number to 4242 4242 4242 4242.",
        "I could not change the name to Bob.",
        "I will change the name to Bob.",
        "I set the notifications to on.",
    ],
)
def test_vague_secret_or_unasserted_values_make_no_setting_claim(answer: str):
    assert not [c for c in _extract(answer).claims if c.text.startswith("The agent set the")]


def test_an_email_value_stays_with_the_email_rule():
    assert _texts(_extract("I changed the email to a@b.test.")) == ["The agent changed the email"]
