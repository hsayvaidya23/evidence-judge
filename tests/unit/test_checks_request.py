from __future__ import annotations

import pytest

from evidence_judge.checks.errors import InvalidPredicateError, PredicateKindMismatchError
from evidence_judge.checks.request import RequestChecker, RequestMatcher, matching_requests
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import PageTextPredicate, RequestPredicate
from evidence_judge.core.verdict import Support
from tests.unit.conftest import (
    ctx,
    forged_run,
    honest_run,
    make_run,
    net,
    uncaptured_network_run,
)

ORDER = RequestPredicate(method="POST", url_contains=["/api/orders"])


async def test_honest_order_is_supported_and_cited() -> None:
    res = await RequestChecker().evaluate(ORDER, ctx(honest_run()))
    assert res.status is Support.SUPPORTED
    assert res.checker == "request"
    assert [c.evidence_id for c in res.citations] == ["net#6"]
    assert res.citations[0].excerpt == "POST /api/orders → 201"
    assert "POST /api/orders → 201 matched" in res.reason


async def test_forged_order_is_contradicted_with_nearest_miss() -> None:
    res = await RequestChecker().evaluate(ORDER, ctx(forged_run()))
    assert res.status is Support.CONTRADICTED
    assert res.reason.startswith(
        "No POST request whose URL contains '/api/orders' with status 2xx "
        "among 4 captured requests (1 mutation)"
    )
    assert "closest was POST /api/cart → 200 (URL lacks '/api/orders')" in res.reason
    assert [c.evidence_id for c in res.citations] == ["net#3"]


async def test_uncaptured_network_is_no_evidence_not_contradicted() -> None:
    res = await RequestChecker().evaluate(ORDER, ctx(uncaptured_network_run()))
    assert res.status is Support.NO_EVIDENCE
    assert "Network traffic was not captured" in res.reason


async def test_found_request_counts_even_when_channel_not_flagged() -> None:
    run = make_run(network=[net(1, "POST", "/api/orders", 201)], captured=frozenset())
    res = await RequestChecker().evaluate(ORDER, ctx(run))
    assert res.status is Support.SUPPORTED


async def test_wrong_status_is_explained() -> None:
    run = make_run(network=[net(1, "POST", "/api/orders", 500)])
    res = await RequestChecker().evaluate(ORDER, ctx(run))
    assert res.status is Support.CONTRADICTED
    assert "status 500, expected 2xx" in res.reason


async def test_exact_status_and_any_status() -> None:
    run = make_run(network=[net(1, "POST", "/api/orders", 201)])
    exact = RequestPredicate(method="POST", url_contains=["/api/orders"], status="200")
    anything = RequestPredicate(method="POST", url_contains=["/api/orders"], status=None)
    assert (await RequestChecker().evaluate(exact, ctx(run))).status is Support.CONTRADICTED
    assert (await RequestChecker().evaluate(anything, ctx(run))).status is Support.SUPPORTED


async def test_body_and_response_contains_are_case_insensitive() -> None:
    pred = RequestPredicate(
        method="post",
        url_contains=["/api/orders"],
        body_contains=["red kettle"],
        response_contains=["CONFIRMED"],
    )
    res = await RequestChecker().evaluate(pred, ctx(honest_run()))
    assert res.status is Support.SUPPORTED


async def test_body_mismatch_is_contradicted() -> None:
    pred = RequestPredicate(method="POST", url_contains=["/api/orders"], body_contains=["blue"])
    res = await RequestChecker().evaluate(pred, ctx(honest_run()))
    assert res.status is Support.CONTRADICTED
    assert "request body lacks 'blue'" in res.reason


async def test_unrecorded_body_is_no_evidence() -> None:
    run = make_run(network=[net(1, "POST", "/api/orders", 201)])
    pred = RequestPredicate(method="POST", url_contains=["/api/orders"], body_contains=["kettle"])
    res = await RequestChecker().evaluate(pred, ctx(run))
    assert res.status is Support.NO_EVIDENCE
    assert "request body not recorded" in res.reason
    assert res.citations[0].evidence_id == "net#1"


async def test_url_regex() -> None:
    pred = RequestPredicate(method="GET", url_regex=r"/order/[A-Z]\d+$")
    assert (await RequestChecker().evaluate(pred, ctx(honest_run()))).status is Support.SUPPORTED
    res = await RequestChecker().evaluate(pred, ctx(forged_run()))
    assert res.status is Support.CONTRADICTED
    assert "whose URL matches /" in res.reason


async def test_multiple_hits_cite_latest_first() -> None:
    run = make_run(network=[net(n, "POST", "/api/cart") for n in range(1, 6)])
    pred = RequestPredicate(method="POST", url_contains=["/api/cart"])
    res = await RequestChecker().evaluate(pred, ctx(run))
    assert [c.evidence_id for c in res.citations] == ["net#5", "net#4", "net#3"]
    assert "(5 matching requests)" in res.reason


async def test_empty_network_has_no_nearest_miss() -> None:
    res = await RequestChecker().evaluate(ORDER, ctx(make_run()))
    assert res.status is Support.CONTRADICTED
    assert "among 0 captured requests (0 mutations)" in res.reason
    assert res.citations == []


def test_matcher_rejects_invalid_predicates() -> None:
    with pytest.raises(InvalidPredicateError):
        RequestMatcher(RequestPredicate(method="GET", status="200-299"))
    with pytest.raises(InvalidPredicateError):
        RequestMatcher(RequestPredicate(url_regex="(oops"))


def test_matching_requests_helper() -> None:
    hits = matching_requests(ORDER, EvidenceIndex(honest_run()))
    assert [h.id for h in hits] == ["net#6"]


async def test_wrong_predicate_kind_raises() -> None:
    with pytest.raises(PredicateKindMismatchError):
        await RequestChecker().evaluate(PageTextPredicate(contains="xyz"), ctx(honest_run()))


TRUNCATED_RESPONSE = (
    '{"status": "pending", "items": [' + "x" * 50 + "…[truncated; body is 50000 bytes]"
)
TRUNCATED_REQUEST = "q=" + "x" * 50 + "…[truncated 900 chars]"


async def test_needle_missing_from_truncated_response_is_no_evidence() -> None:
    event = net(1, "POST", "/api/orders", 201).model_copy(
        update={"response_body": TRUNCATED_RESPONSE}
    )
    pred = RequestPredicate(
        method="POST", url_contains=["/api/orders"], response_contains=["order_id"]
    )
    res = await RequestChecker().evaluate(pred, ctx(make_run(network=[event])))
    assert res.status is Support.NO_EVIDENCE
    assert "response body was truncated before 'order_id'" in res.reason
    assert res.citations[0].evidence_id == "net#1"


async def test_needle_missing_from_truncated_request_body_is_no_evidence() -> None:
    event = net(1, "POST", "/api/search", 200).model_copy(
        update={"request_body": TRUNCATED_REQUEST}
    )
    pred = RequestPredicate(method="POST", body_contains=["kettle"])
    res = await RequestChecker().evaluate(pred, ctx(make_run(network=[event])))
    assert res.status is Support.NO_EVIDENCE


async def test_needle_in_recorded_part_of_truncated_body_still_matches() -> None:
    event = net(1, "POST", "/api/orders", 201).model_copy(
        update={"response_body": TRUNCATED_RESPONSE}
    )
    pred = RequestPredicate(method="POST", response_contains=["pending"])
    res = await RequestChecker().evaluate(pred, ctx(make_run(network=[event])))
    assert res.status is Support.SUPPORTED


async def test_truncation_does_not_excuse_other_misses() -> None:
    event = net(1, "POST", "/api/cart", 201).model_copy(
        update={"response_body": TRUNCATED_RESPONSE}
    )
    pred = RequestPredicate(
        method="POST", url_contains=["/api/orders"], response_contains=["order_id"]
    )
    res = await RequestChecker().evaluate(pred, ctx(make_run(network=[event])))
    assert res.status is Support.CONTRADICTED


async def test_url_contains_ignores_the_query_string() -> None:
    laundered = net(1, "POST", "/api/log?next=/api/orders", 200)
    res = await RequestChecker().evaluate(ORDER, ctx(make_run(network=[laundered])))
    assert res.status is Support.CONTRADICTED
    assert "URL lacks '/api/orders'" in res.reason


async def test_url_contains_matches_scheme_host_and_path() -> None:
    run = make_run(network=[net(1, "POST", "/api/orders?coupon=X#top", 201)])
    pred = RequestPredicate(method="POST", url_contains=["https://shop.test/api/orders"])
    assert (await RequestChecker().evaluate(pred, ctx(run))).status is Support.SUPPORTED


async def test_path_and_query_contains() -> None:
    run = make_run(network=[net(1, "GET", "/api/search?q=red%20kettle&page=2")])
    hit = RequestPredicate(method="GET", path_contains=["/search"], query_contains=["red kettle"])
    assert (await RequestChecker().evaluate(hit, ctx(run))).status is Support.SUPPORTED
    in_query = RequestPredicate(method="GET", path_contains=["kettle"])
    res = await RequestChecker().evaluate(in_query, ctx(run))
    assert res.status is Support.CONTRADICTED
    assert "path lacks 'kettle'" in res.reason
    wrong = RequestPredicate(method="GET", query_contains=["page=3"])
    res = await RequestChecker().evaluate(wrong, ctx(run))
    assert "query lacks 'page=3'" in res.reason


def test_describe_names_path_and_query() -> None:
    matcher = RequestMatcher(RequestPredicate(path_contains=["/a"], query_contains=["b=1"]))
    assert matcher.describe() == (
        "request whose path contains '/a' whose query contains 'b=1' with status 2xx"
    )
    assert matcher.criteria_count == 3


async def test_url_regex_still_sees_the_full_url() -> None:
    run = make_run(network=[net(1, "GET", "/api/search?q=kettle")])
    pred = RequestPredicate(url_regex=r"\?q=kettle$")
    assert (await RequestChecker().evaluate(pred, ctx(run))).status is Support.SUPPORTED
