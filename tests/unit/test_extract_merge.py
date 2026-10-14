import pytest

from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.predicates import PageTextPredicate, RequestPredicate
from evidence_judge.extract.merge import (
    cap_claims,
    coverage,
    dedupe_claims,
    merge_claim_sets,
    renumber,
)

ORDER_POST = RequestPredicate(method="POST", url_contains=["order"])
ORDER_TEXT = PageTextPredicate(contains="1042")


def _claim(text: str, quote: str | None = None, *, key: bool = False, **kw) -> Claim:
    return Claim(
        id=kw.pop("id", "x"),
        text=text,
        quote=quote or text,
        kind=kw.pop("kind", ClaimKind.OUTCOME),
        key=key,
        **kw,
    )


def test_dedupe_merges_near_identical_claims_and_their_predicates():
    claims = [
        _claim("The agent placed the order", predicates=[ORDER_POST]),
        _claim("the agent placed the order.", key=True, predicates=[ORDER_TEXT]),
        _claim("The agent placed an order", predicates=[ORDER_POST]),
        _claim("The price was 24", kind=ClaimKind.VALUE),
    ]
    kept, dropped = dedupe_claims(claims)
    assert dropped == 2
    assert [c.text for c in kept] == ["The agent placed the order", "The price was 24"]
    assert kept[0].key is True
    assert kept[0].predicates == [ORDER_POST, ORDER_TEXT]


def test_dedupe_keeps_distinct_kinds_apart():
    kept, dropped = dedupe_claims(
        [_claim("Order 1042", kind=ClaimKind.STATE), _claim("Order 1042", kind=ClaimKind.OUTCOME)]
    )
    assert (len(kept), dropped) == (2, 0)


def test_cap_prefers_key_claims_and_preserves_order():
    claims = [_claim(f"claim {i}", key=(i % 10 == 9)) for i in range(40)]
    capped, dropped = cap_claims(claims, limit=5)
    assert dropped == 35
    assert [c.text for c in capped] == ["claim 0", "claim 9", "claim 19", "claim 29", "claim 39"]


def test_renumber_assigns_sequential_ids():
    assert [c.id for c in renumber([_claim("a"), _claim("b")])] == ["c1", "c2"]


def test_merge_claim_sets_unions_and_renumbers():
    a = ClaimSet(claims=[_claim("Placed the order", id="c1")], extractor="heuristic")
    b = ClaimSet(
        claims=[_claim("placed the order", id="c1"), _claim("Saw total", id="c2")],
        extractor="llm",
        coverage_note="hallucinated_quotes=1",
    )
    merged = merge_claim_sets(a, b)
    assert [c.id for c in merged.claims] == ["c1", "c2"]
    assert [c.text for c in merged.claims] == ["Placed the order", "Saw total"]
    assert merged.extractor == "heuristic+llm"
    assert merged.coverage_note == "hallucinated_quotes=1"


def test_merge_of_nothing_is_empty():
    merged = merge_claim_sets()
    assert merged.claims == []
    assert merged.extractor == "none"


def test_coverage_counts_claim_like_sentences_with_quotes():
    answer = "I placed the order. Order #1042 was confirmed. The page is blue."
    claims = [_claim("Placed order", "I placed the order.")]
    assert coverage(claims, answer, []) == pytest.approx(0.5)
    claims.append(_claim("Confirmed", "order #1042 was confirmed"))
    assert coverage(claims, answer, []) == pytest.approx(1.0)


def test_coverage_is_full_when_nothing_asserted():
    assert coverage([], "I could not complete checkout.", ["Looking at the page."]) == 1.0


def test_coverage_includes_reasoning_and_partial_quotes():
    reasoning = ["Clicked the Buy now button on the product page."]
    claims = [_claim("Clicked buy", "clicked the buy now button")]
    assert coverage(claims, None, reasoning) == 1.0
    assert coverage([], None, reasoning) == 0.0


def test_dedupe_never_merges_claims_with_different_numbers():
    kept, dropped = dedupe_claims([_claim("Order 1042 shown"), _claim("Order 10423 shown")])
    assert (len(kept), dropped) == (2, 0)
