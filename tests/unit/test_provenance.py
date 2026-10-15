from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_judge.checks.outcome import decide
from evidence_judge.config import Settings
from evidence_judge.core.actions import MAX_INJECTED_CHARS, injected
from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.evidence import ActionEvent, NetworkEvent, PageState, Run
from evidence_judge.core.predicates import PageTextPredicate, RequestPredicate, Scope
from evidence_judge.core.provenance import (
    doubt,
    is_script_url,
    mark_agent_page_navigations,
    script_hash,
    tainted_at,
    tampering,
    unseen_upload,
    untampered,
)
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict, Support
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.ingest import load_browser_use_history

from .conftest import HOST, act, final, honest_run, make_run, net, snap

STOCK = TaskSpec.model_validate(
    {
        "id": "stock",
        "task": "Is the red kettle in stock?",
        "site": "shop.test",
        "expect": [
            {
                "id": "in_stock",
                "any": [{"kind": "page_text", "contains": "In stock", "scope": "any"}],
            }
        ],
    }
)
REWRITE = "document.querySelector('#stock').textContent = 'In stock'"


def _inject(n: int, code: str = REWRITE, url: str = f"{HOST}/p/red-kettle") -> ActionEvent:
    return act(n, "inject", value=injected("evaluate", code), url=url)


def _rewritten_stock() -> list[ActionEvent]:
    return [
        act(1, "goto", value=f"{HOST}/", url="about:blank"),
        act(2, "click", "#red", url=f"{HOST}/"),
        _inject(3),
    ]


async def _judge(
    run: Run, spec: TaskSpec | None = STOCK, claims: ClaimSet | None = None
) -> RunVerdict:
    judge = Judge(Settings(anthropic_api_key=None, cache_dir=None))
    return await judge.judge(run, JudgeOptions(offline=True, spec=spec, claims=claims))


def test_injected_value_names_the_api_and_cuts_the_code() -> None:
    assert injected("evaluate", "  a =\n 1 ") == "evaluate(a = 1)"
    assert injected("setContent", None) == "setContent()"
    long = injected("evaluate", "x" * 500)
    assert long == f"evaluate({'x' * (MAX_INJECTED_CHARS - 1)}…)"


def test_untouched_run_has_no_taint() -> None:
    run = honest_run()

    assert tainted_at(run) is None
    assert tampering(run) is None
    assert untampered(run) is run


def test_untampered_cuts_everything_the_site_did_not_make_alone() -> None:
    mocked = net(4, "GET", "/api/stock.json", response={"stock": "In stock"})
    run = make_run(
        actions=[act(1, "goto", value=f"{HOST}/"), act(5, "click", "#buy", url=f"{HOST}/p")],
        network=[
            net(1, "GET", "/"),
            net(2, "GET", "/p"),
            mocked.model_copy(update={"synthetic": True}),
        ],
        snapshots=[snap(1, "/", "Kettles"), snap(4, "/p", "In stock")],
        final_state=final("/p", "In stock"),
    ).model_copy(update={"initial_state": PageState(url=f"{HOST}/", snapshot_id="snap#1")})

    clean = untampered(run)

    assert tainted_at(run) == 400.0
    assert [n.id for n in clean.network] == ["net#1", "net#2"]
    assert [s.id for s in clean.snapshots] == ["snap#1"]
    assert clean.final_state is None
    assert clean.initial_state == run.initial_state
    assert [(a.id, a.url) for a in clean.actions] == [("act#1", None), ("act#5", None)]
    assert tampering(run) == (
        "the agent's route handler answered or rewrote requests (route.fulfill or "
        f"route.continue with overrides): GET {HOST}/api/stock.json (net#4); evidence from then "
        "on is not the site's"
    )


def test_tampering_names_two_calls_and_counts_the_rest() -> None:
    run = make_run(actions=[_inject(n, f"step{n}()") for n in (1, 2, 3)])

    assert tampering(run) == (
        "the agent ran its own code in the page or tampered with the browser: evaluate(step1()) "
        "(act#1), evaluate(step2()) "
        "(act#2) and 1 more; evidence from then on is not the site's"
    )


async def test_page_rewritten_by_evaluate_cannot_prove_and_caps_the_run() -> None:
    run = make_run(
        actions=_rewritten_stock(),
        network=[net(1, "GET", "/"), net(2, "GET", "/p/red-kettle")],
        snapshots=[snap(1, "/", "Kettles"), snap(2, "/p/red-kettle", "Red Kettle Out of stock")],
        final_state=final("/p/red-kettle", "Red Kettle In stock"),
    ).model_copy(update={"agent_answer": "Yes, it is In stock."})

    verdict = await _judge(run)

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary == (
        "NEEDS_REVIEW: the agent ran its own code in the page or tampered with the browser: "
        f"evaluate({REWRITE}) (act#3); evidence from then on is not the site's, so the run "
        "cannot pass."
    )
    [expectation] = verdict.expectations
    assert expectation.met is None
    assert "it holds only on evidence the agent made itself" in expectation.reason


async def test_evidence_from_before_the_injection_still_proves() -> None:
    run = make_run(
        actions=_rewritten_stock(),
        network=[net(1, "GET", "/"), net(2, "GET", "/p/red-kettle")],
        snapshots=[snap(1, "/", "Kettles"), snap(2, "/p/red-kettle", "Red Kettle In stock")],
        final_state=final("/p/red-kettle", "Red Kettle In stock"),
    )

    verdict = await _judge(run)

    assert [e.met for e in verdict.expectations] == [True]
    assert verdict.outcome is Outcome.NEEDS_REVIEW


async def test_contradiction_before_the_injection_still_fails() -> None:
    spec = TaskSpec.model_validate(
        {
            "id": "buy",
            "task": "Buy a red kettle",
            "site": "shop.test",
            "expect": [
                {
                    "id": "ordered",
                    "all": [{"kind": "request", "method": "POST", "url_contains": ["/api/orders"]}],
                }
            ],
        }
    )
    run = make_run(
        actions=_rewritten_stock(),
        network=[net(1, "GET", "/"), net(2, "GET", "/p/red-kettle")],
        snapshots=[snap(2, "/p/red-kettle", "Red Kettle")],
        final_state=final("/p/red-kettle", "Thank you for your order"),
    )

    verdict = await _judge(run, spec)

    assert verdict.outcome is Outcome.FAIL
    assert verdict.summary.startswith("FAIL: expectation 'ordered' was not met")


async def test_mocked_order_response_cannot_prove_a_claim() -> None:
    order = NetworkEvent(
        id="net#3",
        ts=300.0,
        method="POST",
        url=f"{HOST}/api/orders",
        status=201,
        response_body=json.dumps({"order": "A1"}),
        synthetic=True,
    )
    run = make_run(
        actions=[act(1, "goto", value=f"{HOST}/"), act(2, "click", "#buy", url=f"{HOST}/p")],
        network=[net(1, "GET", "/"), order],
        snapshots=[snap(1, "/", "Kettles")],
        final_state=final("/orders/A1", "Thank you for your order"),
    )
    claim = Claim(
        id="c1",
        text="The order was placed",
        quote="The order was placed",
        kind=ClaimKind.OUTCOME,
        key=True,
        predicates=[
            RequestPredicate(method="POST", url_contains=["/api/orders"]),
            PageTextPredicate(contains="Thank you for your order", scope=Scope.FINAL),
        ],
    )

    verdict = await _judge(run, None, ClaimSet(claims=[claim]))

    [result] = verdict.claims
    assert result.status is Support.NO_EVIDENCE
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert "the agent's route handler answered or rewrote requests" in verdict.summary


def test_decide_puts_tampering_after_a_failure_and_before_everything_else() -> None:
    outcome, score, summary = decide(
        [], [], {}, coverage=None, review_threshold=0.5, tampered="The agent ran code"
    )

    assert (outcome, score) == (Outcome.NEEDS_REVIEW, 0.0)
    assert summary == "NEEDS_REVIEW: the agent ran code, so the run cannot pass."


DATA_PAGE = "data:text/html,<a id=x href=https://shop.test/orders/A77>go</a>"


def test_link_on_an_agent_page_becomes_a_goto_of_its_target() -> None:
    run = make_run(
        actions=[
            act(1, "goto", value=f"{HOST}/", url="about:blank"),
            act(2, "goto", value=DATA_PAGE, url=f"{HOST}/"),
            act(3, "click", "#x", url=DATA_PAGE),
        ],
        network=[net(1, "GET", "/"), net(4, "GET", "/orders/A77")],
        final_state=final("/orders/A77", "Page not found"),
    )

    marked = mark_agent_page_navigations(run)

    assert [(a.id, a.name, a.value) for a in marked.actions] == [
        ("act#1", "goto", f"{HOST}/"),
        ("act#2", "goto", DATA_PAGE),
        ("act#3.goto", "goto", f"{HOST}/orders/A77"),
        ("act#3", "click", None),
    ]
    assert marked.actions[2].ts == 350.0 and marked.actions[2].url == DATA_PAGE
    assert mark_agent_page_navigations(marked) is marked


def test_target_is_the_first_web_url_after_the_action() -> None:
    run = make_run(
        actions=[
            act(1, "click", "#x", url="blob:https://shop.test/1"),
            act(5, "click", url=f"{HOST}/b"),
        ],
        snapshots=[snap(2, "/a", "A")],
        final_state=final("/c", "C"),
    )

    assert mark_agent_page_navigations(run).actions[0].value == f"{HOST}/a"


def test_no_goto_without_a_web_page_after_or_for_goto_and_inject() -> None:
    run = make_run(
        actions=[
            act(1, "goto", value=f"{HOST}/", url="about:blank"),
            _inject(2, url="about:blank"),
            act(3, "click", "#x", url="about:blank"),
        ],
    )

    assert mark_agent_page_navigations(run) is run


async def test_data_url_link_proves_nothing_without_an_ingester() -> None:
    spec = TaskSpec.model_validate(
        {
            "id": "open",
            "task": "Open the details page of order A77",
            "site": "shop.test",
            "expect": [{"id": "on_order", "all": [{"kind": "url", "contains": "/orders/A77"}]}],
        }
    )
    run = make_run(
        actions=[
            act(1, "goto", value=f"{HOST}/", url="about:blank"),
            act(2, "goto", value=DATA_PAGE, url=f"{HOST}/"),
            act(3, "click", "#x", url=DATA_PAGE),
        ],
        network=[net(1, "GET", "/"), net(3, "GET", "/orders/A77")],
        snapshots=[snap(1, "/", "Welcome Orders")],
        final_state=final("/orders/A77", "Page not found"),
    ).model_copy(update={"agent_answer": "Done."})

    verdict = await _judge(run, spec)

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    [expectation] = verdict.expectations
    assert expectation.met is None
    assert "echoes the agent's own input" in expectation.reason


def test_browser_use_evaluate_is_an_inject_action(tmp_path: Path) -> None:
    history = tmp_path / "history.json"
    step = {
        "model_output": {"action": [{"evaluate": {"code": REWRITE}}]},
        "result": [{"extracted_content": "ok"}],
        "state": {"url": f"{HOST}/p/red-kettle", "title": "Red Kettle"},
    }
    history.write_text(json.dumps({"history": [step]}))

    run = load_browser_use_history(history)

    assert [(a.name, a.value) for a in run.actions] == [("inject", f"evaluate({REWRITE})")]
    assert tampering(run) is not None


@pytest.mark.parametrize(
    ("url", "runs"),
    [
        ("javascript:void(0)", True),
        (" \x01JavaScript:alert(1)", True),
        ("java\tscr\nipt:alert(1)", True),
        ("VBScript:msgbox(1)", True),
        ("data:text/html,<p>In stock</p>", False),
        ("blob:https://shop.test/1", False),
        ("about:blank", False),
        ("https://shop.test/javascript:x", False),
        (None, False),
    ],
)
def test_script_urls_are_read_the_way_a_browser_reads_their_scheme(
    url: str | None, runs: bool
) -> None:
    assert is_script_url(url) is runs


def test_browser_use_goto_of_a_javascript_url_is_an_inject_action(tmp_path: Path) -> None:
    history = tmp_path / "history.json"
    code = f"javascript:void({REWRITE})"
    step = {
        "model_output": {"action": [{"go_to_url": {"url": code}}, {"go_to_url": {"url": HOST}}]},
        "result": [{}, {}],
        "state": {"url": f"{HOST}/p/red-kettle", "title": "Red Kettle"},
    }
    history.write_text(json.dumps({"history": [step]}))

    run = load_browser_use_history(history)

    assert [(a.name, a.value) for a in run.actions] == [
        ("inject", f"goto({code})"),
        ("goto", HOST),
    ]
    assert run.actions[0].script_sha256 == script_hash("goto", code)
    assert run.actions[1].script_sha256 is None


INVOICE = TaskSpec.model_validate(
    {
        "id": "invoice",
        "task": "Upload the invoice and pay it",
        "site": "shop.test",
        "expect": [
            {
                "id": "paid",
                "all": [{"kind": "request", "method": "POST", "url_contains": ["/api/pay"]}],
            },
        ],
    }
)


def _uploaded(file_text: str | None = None, *, pay: int = 201) -> Run:
    upload = act(2, "upload", "#file", value="invoice.txt", url=f"{HOST}/upload")
    paid = net(3, "POST", "/api/pay", pay).model_copy(update={"response_body": "Paid in full"})
    return make_run(
        actions=[
            act(1, "goto", value=f"{HOST}/upload", url="about:blank"),
            upload.model_copy(update={"file_text": file_text}),
            act(3, "click", "#pay", url=f"{HOST}/upload"),
        ],
        network=[net(1, "GET", "/upload"), paid],
        snapshots=[snap(1, "/upload", "Upload invoice"), snap(3, "/upload", "Paid in full")],
        final_state=final("/upload", "Paid in full"),
    )


def test_an_upload_the_recording_does_not_hold_cuts_page_text_but_not_requests() -> None:
    run = _uploaded()

    cut = untampered(run)

    assert unseen_upload(run) == (
        "the agent uploaded invoice.txt (act#2) whose contents are not in the recording; page "
        "text and response bodies from then on may show them"
    )
    assert doubt(run) == unseen_upload(run) and tampering(run) is None
    assert [s.id for s in cut.snapshots] == ["snap#1"] and cut.final_state is None
    assert [(n.method, n.status, n.response_body) for n in cut.network] == [
        ("GET", 200, None),
        ("POST", 201, None),
    ]
    assert [a.url for a in cut.actions] == ["about:blank", None, None]
    assert untampered(_uploaded("Invoice 12")) == _uploaded("Invoice 12")


def test_an_upload_naming_no_file_is_unseen_too() -> None:
    run = _uploaded()
    nameless = run.model_copy(
        update={
            "actions": [
                a.model_copy(update={"value": None}) if a.name == "upload" else a
                for a in run.actions
            ]
        }
    )

    assert unseen_upload(nameless) == (
        "the agent uploaded a file (act#2) whose contents are not in the recording; page "
        "text and response bodies from then on may show them"
    )
    assert untampered(nameless).final_state is None


async def test_after_an_unseen_upload_requests_still_prove_and_page_text_does_not() -> None:
    paid = TaskSpec.model_validate(
        {
            **INVOICE.model_dump(),
            "expect": [
                *INVOICE.model_dump()["expect"],
                {
                    "id": "shown",
                    "any": [{"kind": "page_text", "contains": "Paid in full", "scope": "final"}],
                },
            ],
        }
    )

    request_only = await _judge(_uploaded(), INVOICE)
    with_text = await _judge(_uploaded(), paid)
    declined = await _judge(_uploaded(pay=402), INVOICE)
    known = await _judge(_uploaded("Invoice 12"), paid)

    assert request_only.outcome is Outcome.PASS
    assert with_text.outcome is Outcome.NEEDS_REVIEW
    assert [e.met for e in with_text.expectations] == [True, None]
    assert "whose contents are not in the recording" in with_text.expectations[1].reason
    assert declined.outcome is Outcome.FAIL
    assert known.outcome is Outcome.PASS
