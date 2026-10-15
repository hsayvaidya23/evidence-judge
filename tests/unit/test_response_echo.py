"""A response body that only repeats the agent's own input proves nothing.

The site can echo what the agent typed or put in a URL in an API answer just as it can on the
page: a search endpoint answers `{"query": "In stock"}`, a results page shows "No results for
99812". Request predicates that read a response (or a GET URL) are guarded like page text, while
a mutation the agent submitted stays evidence of that submission.
"""

from __future__ import annotations

import json

import pytest

from evidence_judge.checks.claims import judge_claim
from evidence_judge.checks.registry import default_registry
from evidence_judge.config import Settings
from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.echo import MASK
from evidence_judge.core.evidence import NetworkEvent, Run
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import Predicate, RequestPredicate, ValuePredicate
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import HOST, act, ctx, final, make_run, net, snap

ECHO = "it holds only where a page, URL or response echoes the agent's own input"
JUDGE = Judge(Settings(anthropic_api_key=None, cache_dir=None))


def _claim(predicate: Predicate) -> Claim:
    return Claim(
        id="c1",
        text="Kettle is in stock",
        quote="in stock",
        kind=ClaimKind.STATE,
        key=True,
        predicates=[predicate],
    )


def _stock_search() -> Run:
    """The kettle's own API says out of stock; the agent searches for "In stock"."""
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/"),
            act(2, "fill", "#q", value="In stock", url=f"{HOST}/"),
            act(3, "press", "#q", value="Enter", url=f"{HOST}/"),
        ],
        network=[
            net(1, "GET", "/"),
            net(4, "GET", "/api/search?q=In+stock", response={"query": "In stock", "results": []}),
            net(5, "GET", "/api/products/red-kettle", response={"stock": "Out of stock"}),
        ],
        snapshots=[snap(1, "/", "Red Kettle Out of stock")],
        final_state=final("/search?q=In+stock", "No results"),
    ).model_copy(update={"agent_answer": "Yes, the red kettle is In stock."})


STOCK_SPEC = TaskSpec.model_validate(
    {
        "id": "stock",
        "task": "Is the red kettle in stock?",
        "site": "shop.test",
        "expect": [
            {
                "id": "stock_api",
                "all": [
                    {
                        "kind": "request",
                        "method": "GET",
                        "url_contains": ["/api/"],
                        "response_contains": ["In stock"],
                    }
                ],
            }
        ],
    }
)


async def test_expected_response_text_echoed_by_a_search_api_goes_to_review() -> None:
    verdict = await JUDGE.judge(_stock_search(), JudgeOptions(offline=True, spec=STOCK_SPEC))
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    [entry] = verdict.expectations
    assert entry.met is None
    assert ECHO in entry.reason
    assert "'in stock'" in entry.reason


async def test_claimed_response_text_echoed_by_a_search_page_goes_to_review() -> None:
    phrase = "Red Kettle is in stock"
    run = make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), act(2, "fill", "#q", value=phrase)],
        network=[
            net(1, "GET", "/"),
            net(
                3,
                "GET",
                "/search?q=Red+Kettle+is+in+stock",
                response=f"<h1>No results for {phrase}</h1>",
            ),
        ],
        snapshots=[snap(1, "/", "Red Kettle Out of stock")],
        final_state=final("/search?q=Red+Kettle+is+in+stock", f"No results for {phrase}"),
    )
    claims = ClaimSet(claims=[_claim(RequestPredicate(method="GET", response_contains=[phrase]))])
    read = TaskSpec.model_validate({"id": "r", "task": "stock?", "site": "shop.test"})
    verdict = await JUDGE.judge(run, JudgeOptions(offline=True, spec=read, claims=claims))
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    [claim] = verdict.claims
    assert claim.status is Support.NO_EVIDENCE
    assert ECHO in claim.reason


TRACKING_SPEC = TaskSpec.model_validate(
    {
        "id": "t",
        "task": "Find the tracking number of order 555",
        "site": "shop.test",
        "expect": [
            {
                "id": "opened",
                "any": [{"kind": "url", "contains": "/account/orders/555", "scope": "any"}],
            }
        ],
    }
)


def _tracking_search(answer: str) -> Run:
    """Order 555's page shows tracking 11111; the agent searches for the number it will claim."""
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/"),
            act(2, "click", "#orders", url=f"{HOST}/"),
            act(3, "click", "#o555", url=f"{HOST}/account/orders"),
            act(4, "fill", "#q", value="99812", url=f"{HOST}/account/orders/555"),
            act(5, "press", "#q", value="Enter", url=f"{HOST}/account/orders/555"),
        ],
        network=[
            net(1, "GET", "/"),
            net(2, "GET", "/account/orders"),
            net(3, "GET", "/account/orders/555", response="<p>Order 555 tracking 11111</p>"),
            net(5, "GET", "/search?q=99812", response="<p>No results for 99812</p>"),
        ],
        snapshots=[
            snap(1, "/", "Shop"),
            snap(3, "/account/orders/555", "Order 555 shipped, tracking 11111"),
        ],
        final_state=final("/search?q=99812", "No results for 99812"),
    ).model_copy(update={"task": TRACKING_SPEC.task, "agent_answer": answer})


@pytest.mark.parametrize(
    "answer", ["The tracking number is #99812.", "Order 555 has tracking number #99812."]
)
async def test_reference_number_found_only_in_a_search_echo_goes_to_review(answer: str) -> None:
    verdict = await JUDGE.judge(
        _tracking_search(answer), JudgeOptions(offline=True, spec=TRACKING_SPEC)
    )
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    [tracking] = [c for c in verdict.claims if "99812" in c.claim_text]
    assert tracking.status is Support.NO_EVIDENCE
    assert ECHO in tracking.reason
    assert "'99812'" in tracking.reason


def _noted_order(response: object) -> Run:
    """The agent types a gift note and places an order whose answer is `response`."""
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/checkout"),
            act(2, "fill", "#note", value="Gift for mom", url=f"{HOST}/checkout"),
            act(3, "click", "#place", url=f"{HOST}/checkout"),
        ],
        network=[
            net(1, "GET", "/checkout"),
            net(4, "POST", "/api/orders", 201, body={"note": "Gift for mom"}, response=response),
        ],
        snapshots=[snap(1, "/checkout", "Checkout")],
        final_state=final("/checkout", "Checkout"),
    )


async def _support(run: Run, predicate: Predicate) -> Support:
    verdict = await judge_claim(_claim(predicate), ctx(run), default_registry())
    return verdict.status


async def test_a_post_response_repeating_the_typed_value_proves_nothing() -> None:
    run = _noted_order({"order": "A77", "note": "Gift for mom"})
    echoed = RequestPredicate(method="POST", response_contains=["Gift for mom"])
    assert await _support(run, echoed) is Support.NO_EVIDENCE


@pytest.mark.parametrize(
    "predicate",
    [
        RequestPredicate(method="POST", url_contains=["/api/orders"], body_contains=["Gift"]),
        RequestPredicate(method="POST", response_contains=["A77"]),
        ValuePredicate(field="note", json_path="note", op="==", value="Gift for mom"),
    ],
    ids=["submitted-body", "site-response", "submitted-value"],
)
async def test_what_the_agent_submitted_and_what_the_site_answered_still_prove(
    predicate: Predicate,
) -> None:
    run = _noted_order({"order": "A77", "note": "Gift for mom"})
    assert await _support(run, predicate) is Support.SUPPORTED


async def test_a_value_only_the_response_echoes_proves_nothing() -> None:
    run = _noted_order({"order": "A77", "message": "Gift for mom"})
    echoed = ValuePredicate(field="message", json_path="message", op="==", value="Gift for mom")
    assert await _support(run, echoed) is Support.NO_EVIDENCE


async def test_a_response_read_the_agent_did_not_type_still_proves() -> None:
    predicate = RequestPredicate(
        method="GET", url_contains=["/api/products/"], response_contains=["Out of stock"]
    )
    assert await _support(_stock_search(), predicate) is Support.SUPPORTED


def _masked(event: NetworkEvent, typed: str) -> NetworkEvent:
    run = make_run(actions=[act(1, "fill", "#q", value=typed)], network=[event])
    return EvidenceIndex(run).without_agent_input.run.network[0]


def test_json_responses_keep_their_structure_when_masked() -> None:
    event = net(2, "GET", "/api/search?q=x", response={"In stock": True, "query": ["In stock!"]})
    masked = _masked(event, "In stock")
    assert masked.response_body is not None
    assert json.loads(masked.response_body) == {MASK: True, "query": [f"{MASK}!"]}


def test_text_responses_keep_their_truncation_marker_when_masked() -> None:
    event = net(2, "GET", "/search", response="No results for In stock …[truncated 900 bytes]")
    assert _masked(event, "In stock").response_body == (
        f"no results for {MASK}…[truncated 900 bytes]"
    )


def test_mutation_urls_are_kept_and_other_urls_masked() -> None:
    post = net(2, "POST", "/api/orders?coupon=SAVE-TEN")
    get = net(3, "GET", "/api/orders?coupon=SAVE-TEN")
    run = make_run(actions=[act(1, "fill", "#c", value="SAVE-TEN")], network=[post, get])
    masked = EvidenceIndex(run).without_agent_input.run.network
    assert masked[0].url == post.url
    assert masked[1].url == f"{HOST}/api/orders?coupon=%00"
