import pytest

from evidence_judge.extract.text import (
    is_claim_like,
    normalize,
    quote_in,
    sentences,
    split_sentences,
)


def test_normalize_folds_case_whitespace_and_typography():
    assert (
        normalize("Order\u00a0 #1042\n\u201cConfirmed\u201d \u2014 DONE")
        == 'order #1042 "confirmed" - done'
    )


def test_quote_in_tolerates_whitespace_case_and_trailing_punctuation():
    source = "I placed the order.\nOrder   #1042 was Confirmed!"
    assert quote_in("order #1042 was confirmed", source)
    assert quote_in("I placed the order.", source)
    assert not quote_in("order #9999 was confirmed", source)
    assert not quote_in("   ", source)


def test_split_sentences_keeps_decimals_and_abbreviations():
    text = "The price was $24.99 total. Order No. 1042 is confirmed! Done?\n\n- Next item"
    assert split_sentences(text) == [
        "The price was $24.99 total.",
        "Order No. 1042 is confirmed!",
        "Done?",
        "Next item",
    ]


def test_sentences_tag_origin():
    result = sentences("I placed the order.", ["Clicked buy. Waited."])
    assert [(s.text, s.origin) for s in result] == [
        ("I placed the order.", "answer"),
        ("Clicked buy.", "reasoning"),
        ("Waited.", "reasoning"),
    ]


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("I placed the order.", True),
        ("The order was placed successfully.", True),
        ("I added the item to the cart, but could not complete checkout.", True),
        ("I could not complete checkout.", False),
        ("The order was not placed.", False),
        ("I will place the order next.", False),
        ("I'm going to submit the form.", False),
        ("I never submitted the form.", False),
        ("The page shows a list of products.", False),
    ],
)
def test_is_claim_like(sentence: str, expected: bool):
    assert is_claim_like(sentence) is expected


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("Order No. 1042 is confirmed.", True),
        ("Once I placed the order, the page changed.", True),
        ("Next I clicked the buy button.", True),
        ("I couldn\u2019t submit the form.", False),
        ("No order was placed.", False),
        ("Nothing was purchased.", False),
        ("The kettle is out of stock; nothing was ordered and nothing was added.", False),
        ("I never logged in, added it to the cart or placed an order.", False),
        ("Never logged in, added it to the cart or placed an order.", False),
        ("I did not open, edit and save it.", False),
        ("Nothing was changed, deleted or sent.", False),
        ("No problems, placed the order.", True),
        ("I didn't hit any errors, and ordered the kettle.", True),
        ("I bought the kettle, and nothing else was changed.", True),
        ("Ran `grep -v not` and saved the file.", True),
    ],
)
def test_is_claim_like_edge_cases(sentence: str, expected: bool):
    assert is_claim_like(sentence) is expected
