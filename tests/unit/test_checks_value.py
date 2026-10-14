from __future__ import annotations

import pytest

from evidence_judge.checks.errors import InvalidPredicateError
from evidence_judge.checks.value import ValueChecker, format_value
from evidence_judge.core.predicates import RequestPredicate, ValuePredicate
from evidence_judge.core.verdict import Support
from tests.unit.conftest import (
    constraint_violation_run,
    ctx,
    final,
    forged_run,
    honest_run,
    make_run,
    net,
    snap,
    uncaptured_network_run,
)

ORDERS = RequestPredicate(method="POST", url_contains=["/api/orders"])
PRICE_CAP = ValuePredicate(
    field="price", json_path="items.0.price", in_request=ORDERS, op="<=", value=30
)


async def test_price_within_cap_is_supported() -> None:
    res = await ValueChecker().evaluate(PRICE_CAP, ctx(honest_run()))
    assert res.status is Support.SUPPORTED
    assert res.citations[0].evidence_id == "net#6"
    assert res.reason == (
        "price was 24.0 in the request of POST /api/orders → 201, satisfying <= 30"
    )


async def test_price_over_cap_is_contradicted_with_observed_value() -> None:
    res = await ValueChecker().evaluate(PRICE_CAP, ctx(constraint_violation_run()))
    assert res.status is Support.CONTRADICTED
    assert res.reason == "price was 45.0 in the request of POST /api/orders → 201, expected <= 30"
    assert res.citations[0].evidence_id == "net#6"


async def test_default_candidates_are_mutations() -> None:
    pred = ValuePredicate(field="price", json_path="items.0.price", op="<=", value=30)
    res = await ValueChecker().evaluate(pred, ctx(constraint_violation_run()))
    assert res.status is Support.CONTRADICTED


async def test_reads_response_body_when_request_lacks_path() -> None:
    pred = ValuePredicate(field="status", json_path="status", in_request=ORDERS, value="confirmed")
    res = await ValueChecker().evaluate(pred, ctx(honest_run()))
    assert res.status is Support.SUPPORTED
    assert "the response of POST /api/orders" in res.reason


async def test_missing_request_is_no_evidence_not_contradicted() -> None:
    res = await ValueChecker().evaluate(PRICE_CAP, ctx(forged_run()))
    assert res.status is Support.NO_EVIDENCE
    assert res.reason == (
        "No POST request whose URL contains '/api/orders' with a 2xx or 3xx status was captured, "
        "so 'price' could not be read"
    )


async def test_uncaptured_network_is_no_evidence() -> None:
    res = await ValueChecker().evaluate(PRICE_CAP, ctx(uncaptured_network_run()))
    assert res.status is Support.NO_EVIDENCE
    assert res.reason.startswith("Network traffic was not captured")


async def test_path_absent_from_bodies() -> None:
    pred = ValuePredicate(field="coupon", json_path="coupon.code", in_request=ORDERS, value="X")
    res = await ValueChecker().evaluate(pred, ctx(honest_run()))
    assert res.status is Support.NO_EVIDENCE
    assert "None of 1 candidate request has 'coupon.code'" in res.reason


async def test_page_regex_reads_every_page() -> None:
    pred = ValuePredicate(field="total", page_regex=r"total \$([\d.]+)", op="<=", value=30)
    res = await ValueChecker().evaluate(pred, ctx(honest_run()))
    assert res.status is Support.SUPPORTED
    assert res.citations[0].evidence_id == "final"
    res = await ValueChecker().evaluate(pred, ctx(constraint_violation_run()))
    assert res.status is Support.CONTRADICTED
    assert "total was '45.00' in the page /order/A1, expected <= 30" in res.reason


async def test_page_regex_violation_on_an_earlier_page_is_not_hidden_by_the_final_page() -> None:
    run = make_run(
        snapshots=[snap(1, "/checkout", "Steel kettle Total: $45.00 Place order")],
        final_state=final("/order/1", "Thank you! Use code SAVE for $5 off next time"),
    )
    pred = ValuePredicate(field="price", page_regex=r"\$([\d.]+)", op="<=", value=30)
    res = await ValueChecker().evaluate(pred, ctx(run))
    assert res.status is Support.CONTRADICTED
    assert "45.00" in res.reason


async def test_page_regex_reads_every_match_on_a_page() -> None:
    run = make_run(final_state=final("/checkout", "Shipping $5.00 Subtotal $40.00 Total $45.00"))
    pred = ValuePredicate(field="price", page_regex=r"\$([\d.]+)", op="<=", value=30)
    res = await ValueChecker().evaluate(pred, ctx(run))
    assert res.status is Support.CONTRADICTED
    assert "45.00" in res.reason


async def test_page_regex_anchored_on_a_label_reads_only_that_value() -> None:
    run = make_run(final_state=final("/checkout", "Shipping $5.00 Total: $25.00"))
    pred = ValuePredicate(field="total", page_regex=r"Total:\s*\$([\d.,]+)", op="<=", value=30)
    res = await ValueChecker().evaluate(pred, ctx(run))
    assert res.status is Support.SUPPORTED
    assert "25.00" in res.reason


async def test_differing_page_values_are_noted() -> None:
    run = make_run(
        snapshots=[snap(1, "/cart", "Total: $20.00")],
        final_state=final("/checkout", "Total: $25.00"),
    )
    pred = ValuePredicate(field="total", page_regex=r"Total:\s*\$([\d.,]+)", op="<=", value=30)
    res = await ValueChecker().evaluate(pred, ctx(run))
    assert res.status is Support.SUPPORTED
    assert "observed values differ: '20.00', '25.00'" in res.reason


async def test_page_regex_without_group_uses_whole_match() -> None:
    pred = ValuePredicate(field="order", page_regex=r"order a\d", op="contains", value="A1")
    res = await ValueChecker().evaluate(pred, ctx(honest_run()))
    assert res.status is Support.SUPPORTED


async def test_any_violation_wins_over_other_observations() -> None:
    run = make_run(
        network=[
            net(1, "POST", "/api/cart", body={"price": 20}),
            net(2, "POST", "/api/cart", body={"price": 50}),
        ]
    )
    pred = ValuePredicate(field="price", json_path="price", op="<=", value=30)
    res = await ValueChecker().evaluate(pred, ctx(run))
    assert res.status is Support.CONTRADICTED
    assert "price was 50.0" in res.reason
    assert "1 other observation satisfied it" in res.reason


async def test_unparseable_value_is_no_evidence_for_ordering() -> None:
    run = make_run(network=[net(1, "POST", "/api/orders", 201, body={"total": "N/A"})])
    pred = ValuePredicate(field="total", json_path="total", op="<=", value=1000)
    res = await ValueChecker().evaluate(pred, ctx(run))
    assert res.status is Support.NO_EVIDENCE
    assert "cannot compare 'N/A' numerically" in res.reason


async def test_spaced_thousands_are_compared_numerically() -> None:
    run = make_run(network=[net(1, "POST", "/api/orders", 201, body={"total": "1 500"})])
    pred = ValuePredicate(field="total", json_path="total", op="<=", value=1000)
    res = await ValueChecker().evaluate(pred, ctx(run))
    assert res.status is Support.CONTRADICTED


async def test_violation_wins_over_unparseable_observation() -> None:
    run = make_run(
        network=[
            net(1, "POST", "/api/cart", body={"price": "N/A"}),
            net(2, "POST", "/api/cart", body={"price": 50}),
        ]
    )
    pred = ValuePredicate(field="price", json_path="price", op="<=", value=30)
    res = await ValueChecker().evaluate(pred, ctx(run))
    assert res.status is Support.CONTRADICTED


async def test_form_encoded_body() -> None:
    run = make_run(network=[net(1, "POST", "/api/orders", 201, body="sku=K-1&qty=2")])
    pred = ValuePredicate(field="qty", json_path="qty", in_request=ORDERS, op="==", value=2)
    assert (await ValueChecker().evaluate(pred, ctx(run))).status is Support.SUPPORTED


def test_format_value() -> None:
    assert format_value(45) == "45.0"
    assert format_value("$45.00") == "'$45.00'"
    assert format_value({"a": 1}) == "'{\"a\": 1}'"


@pytest.mark.parametrize(
    "pred",
    [
        ValuePredicate(field="x", page_regex=r"(a)(b)", value=1),
        ValuePredicate(field="x", page_regex=r"(", value=1),
    ],
)
async def test_invalid_value_predicates(pred: ValuePredicate) -> None:
    with pytest.raises(InvalidPredicateError):
        await ValueChecker().evaluate(pred, ctx(honest_run()))


TRUNCATED_JSON = '{"items": [{"sku": "K-1", "na' + "…[truncated; body is 90000 bytes]"


async def test_truncated_json_body_is_unrecorded_not_contradicted() -> None:
    event = net(1, "POST", "/api/orders", 201).model_copy(update={"request_body": TRUNCATED_JSON})
    res = await ValueChecker().evaluate(PRICE_CAP, ctx(make_run(network=[event])))
    assert res.status is Support.NO_EVIDENCE
    assert "truncated" in res.reason


async def test_truncated_candidate_keeps_a_guardrail_from_passing() -> None:
    readable = net(1, "POST", "/api/orders", 201, body={"items": [{"price": 24}]})
    truncated = net(2, "POST", "/api/orders", 201).model_copy(
        update={"request_body": TRUNCATED_JSON}
    )
    res = await ValueChecker().evaluate(PRICE_CAP, ctx(make_run(network=[readable, truncated])))
    assert res.status is Support.NO_EVIDENCE
    assert "net#2" in res.reason


@pytest.mark.parametrize(("status", "failed"), [(500, False), (409, False), (None, True)])
async def test_default_candidates_skip_mutations_that_did_not_take_effect(
    status: int | None, failed: bool
) -> None:
    run = make_run(
        network=[
            net(1, "POST", "/api/orders", status, body={"price": 45}, failed=failed),
            net(2, "POST", "/api/orders", 201, body={"price": 24}),
        ]
    )
    pred = ValuePredicate(field="price", json_path="price", op="<=", value=30)
    res = await ValueChecker().evaluate(pred, ctx(run))
    assert res.status is Support.SUPPORTED, res.reason
    assert [c.evidence_id for c in res.citations] == ["net#2"]


async def test_redirected_mutation_is_a_default_candidate() -> None:
    run = make_run(network=[net(1, "POST", "/api/orders", 303, body={"price": 45})])
    pred = ValuePredicate(field="price", json_path="price", op="<=", value=30)
    res = await ValueChecker().evaluate(pred, ctx(run))
    assert res.status is Support.CONTRADICTED
