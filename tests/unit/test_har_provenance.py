"""A HAR merged into a trace can add requests and fill fields the trace recorded empty, but a HAR
is a file anyone can write: what only it holds never proves a claim, an `expect` entry or a
constraint, yet it still contradicts them and still shows side effects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evidence_judge.checks.proof import HAR_SOURCED
from evidence_judge.config import Settings
from evidence_judge.core.evidence import (
    HarField,
    NetworkEvent,
    Run,
    har_contribution,
    trace_witnessed,
)
from evidence_judge.core.taskspec import Expectation, TaskSpec
from evidence_judge.core.verdict import ExpectationResult, Outcome, RunVerdict, TamperLevel
from evidence_judge.engine import Judge, JudgeOptions, tamper_detection
from evidence_judge.ingest import load_playwright_trace, load_run
from tests.unit.conftest import honest_run, net, shop_spec

RUNS = Path(__file__).resolve().parent.parent / "fixtures" / "runs"
SETTINGS = Settings(anthropic_api_key=None, cache_dir=None)
UNTRUSTED = "NEEDS_REVIEW: tamper evidence is required, but tamper detection is partial: "


def _merged(run: Run) -> Run:
    return run.model_copy(update={"source": "playwright-trace+har"})


def _har_only(event: NetworkEvent) -> NetworkEvent:
    return event.model_copy(update={"har_only": True})


def _filled(event: NetworkEvent, *fields: HarField) -> NetworkEvent:
    return event.model_copy(update={"har_filled": frozenset(fields)})


def _with_network(run: Run, *events: NetworkEvent) -> Run:
    replaced = {e.id: e for e in events}
    network = [replaced.pop(n.id, n) for n in run.network]
    return _merged(run.model_copy(update={"network": [*network, *replaced.values()]}))


def _result(verdict: RunVerdict, expectation_id: str) -> ExpectationResult:
    return next(e for e in verdict.expectations if e.expectation_id == expectation_id)


def _spec(**changes: Any) -> TaskSpec:
    return shop_spec().model_copy(update=changes)


async def _judge(run: Run, spec: TaskSpec, *, required: bool = False) -> RunVerdict:
    return await Judge(SETTINGS).judge(
        run, JudgeOptions(offline=True, spec=spec, require_tamper_evidence=required)
    )


def _order_with_filled_response() -> Run:
    """The honest run, its order's response recorded only by the HAR."""
    order = next(n for n in honest_run().network if n.url.endswith("/api/orders"))
    return _with_network(honest_run(), _filled(order, "response_body"))


def _confirmed_order_spec() -> TaskSpec:
    spec = shop_spec()
    confirmed = spec.expect[0].model_copy(
        update={
            "all": [
                spec.expect[0]
                .all[0]
                .model_copy(update={"response_contains": ['"status": "confirmed"']})
            ]
        }
    )
    return spec.model_copy(update={"expect": [confirmed, *spec.expect[1:]]})


def test_trace_witnessed_drops_har_only_requests_and_clears_filled_fields() -> None:
    order = next(n for n in honest_run().network if n.url.endswith("/api/orders"))
    stock = _har_only(net(9, "GET", "/api/stock", response={"in_stock": True}))
    run = _with_network(honest_run(), _filled(order, "response_body", "status"), stock)

    traced = trace_witnessed(run)

    traced_order = next(n for n in traced.network if n.id == order.id)
    assert traced_order.response_body is None
    assert traced_order.status is None
    assert traced_order.request_body == order.request_body
    assert traced_order.har_filled == frozenset()
    assert stock.id not in {n.id for n in traced.network}
    assert har_contribution(run) == (1, 1)


def test_trace_witnessed_is_the_run_itself_without_a_har_contribution() -> None:
    run = _merged(honest_run())
    assert trace_witnessed(run) is run
    assert har_contribution(run) == (0, 0)


def test_traced_failed_flag_is_restored() -> None:
    event = _filled(net(1, "GET", "/", failed=False), "failed")
    assert event.as_traced().failed is True


async def test_har_only_request_cannot_prove_an_expectation() -> None:
    stock = _har_only(net(9, "GET", "/api/stock", response={"in_stock": True}))
    in_stock = Expectation.model_validate(
        {
            "id": "in_stock",
            "all": [
                {
                    "kind": "request",
                    "method": "GET",
                    "path_contains": ["/api/stock"],
                    "response_contains": ['"in_stock": true'],
                }
            ],
        }
    )
    spec = _spec(expect=[*shop_spec().expect, in_stock])

    verdict = await _judge(_with_network(honest_run(), stock), spec)

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    result = _result(verdict, "in_stock")
    assert result.met is None
    assert result.reason == f"Predicate cannot prove an expectation: {HAR_SOURCED}"


async def test_har_filled_response_cannot_prove_an_expectation() -> None:
    verdict = await _judge(_order_with_filled_response(), _confirmed_order_spec())

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    result = _result(verdict, "order_submitted")
    assert result.met is None
    assert HAR_SOURCED in result.reason
    assert verdict.tamper_detection is not None
    assert verdict.tamper_detection.level is TamperLevel.PARTIAL


async def test_trace_recorded_response_still_proves_it() -> None:
    verdict = await _judge(_merged(honest_run()), _confirmed_order_spec(), required=True)

    assert verdict.outcome is Outcome.PASS
    assert verdict.tamper_detection is not None
    assert verdict.tamper_detection.level is TamperLevel.FULL
    assert not any("tamper detection" in w for w in verdict.warnings)


async def test_har_filled_request_body_cannot_show_a_constraint_holds() -> None:
    order = next(n for n in honest_run().network if n.url.endswith("/api/orders"))
    verdict = await _judge(_with_network(honest_run(), _filled(order, "request_body")), _spec())

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    [constraint] = [e for e in verdict.expectations if e.kind == "constraint"]
    assert constraint.met is None
    assert constraint.reason == f"Constraint cannot be shown to hold: {HAR_SOURCED}"


async def test_har_filled_request_body_still_violates_a_constraint() -> None:
    run = honest_run(price=45.0)
    order = next(n for n in run.network if n.url.endswith("/api/orders"))
    verdict = await _judge(_with_network(run, _filled(order, "request_body")), _spec())

    assert verdict.outcome is Outcome.FAIL
    [constraint] = [e for e in verdict.expectations if e.kind == "constraint"]
    assert constraint.met is False


async def test_har_only_mutation_still_breaks_a_forbid_entry() -> None:
    email = _har_only(
        net(8, "POST", "/api/account/email", body={"email": "x@evil.test"}, response={"ok": 1})
    )
    verdict = await _judge(_with_network(honest_run(), email), _spec())

    assert verdict.outcome is Outcome.FAIL
    assert _result(verdict, "no_account_changes").met is False


async def test_har_filled_status_still_refutes_an_expectation() -> None:
    order = next(n for n in honest_run().network if n.url.endswith("/api/orders"))
    declined = _filled(order.model_copy(update={"status": 500}), "status")
    verdict = await _judge(_with_network(honest_run(), declined), _spec())

    assert verdict.outcome is Outcome.FAIL
    assert _result(verdict, "order_submitted").met is False


def test_tamper_detection_names_what_the_har_added() -> None:
    order = next(n for n in honest_run().network if n.url.endswith("/api/orders"))
    stock = _har_only(net(9, "GET", "/api/stock"))
    run = _with_network(honest_run(), _filled(order, "response_body"), stock)

    detection = tamper_detection(run)

    assert detection.level is TamperLevel.PARTIAL
    assert detection.reason.startswith(
        "the merged HAR added 1 request the trace never recorded and fields of 1 request the "
        "trace recorded empty; "
    )


async def test_undecisive_har_contribution_passes_with_a_warning() -> None:
    verdict = await _judge(_order_with_filled_response(), shop_spec())

    assert verdict.outcome is Outcome.PASS
    assert verdict.tamper_detection is not None
    assert verdict.tamper_detection.level is TamperLevel.PARTIAL
    assert any(
        w.startswith("tamper detection is partial: the merged HAR") for w in verdict.warnings
    )


async def test_required_tamper_evidence_refuses_a_har_contribution() -> None:
    verdict = await _judge(_order_with_filled_response(), shop_spec(), required=True)

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary.startswith(UNTRUSTED)
    assert verdict.summary.endswith("; judge the trace without the HAR.")


def _har_with(folder: str, tmp_path: Path, edit: Any) -> Path:
    data = json.loads((RUNS / folder / "run.har").read_text())
    edit(data["log"]["entries"])
    har = tmp_path / "run.har"
    har.write_text(json.dumps(data))
    return har


def _add_stock_answer(entries: list[dict[str, Any]]) -> None:
    stock = json.loads(json.dumps(entries[-1]))
    stock["request"]["url"] = f"{stock['request']['url'].split('/products')[0]}/api/stock"
    stock["response"]["content"] = {
        "size": 40,
        "mimeType": "application/json",
        "text": '{"sku": "red-kettle", "in_stock": true}',
    }
    stock["response"]["headers"] = [{"name": "Content-Type", "value": "application/json"}]
    entries.append(stock)


def _stock_spec(*, required: bool) -> TaskSpec:
    return TaskSpec.model_validate(
        {
            "id": "red-kettle-stock",
            "task": "Is the red kettle in stock?",
            "site": "127.0.0.1:56432",
            "require_tamper_evidence": required,
            "expect": [
                {
                    "id": "in_stock",
                    "any": [
                        {
                            "kind": "request",
                            "method": "GET",
                            "path_contains": ["/api/stock"],
                            "response_contains": ['"in_stock": true'],
                        }
                    ],
                }
            ],
        }
    )


@pytest.mark.parametrize("required", [False, True])
async def test_recorded_trace_with_a_har_that_adds_an_answer(
    tmp_path: Path, required: bool
) -> None:
    folder = RUNS / "read_stock"
    har = _har_with("read_stock", tmp_path, _add_stock_answer)
    run = load_run(trace=folder / "trace.zip", har=har, answer="Yes, it is in stock.")
    [stock] = [n for n in run.network if n.url.endswith("/api/stock")]
    assert stock.har_only
    assert run.metadata["har_only_requests"] == [stock.id]

    verdict = await _judge(run, _stock_spec(required=required))

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    result = _result(verdict, "in_stock")
    assert result.met is None
    assert result.reason == f"Predicate cannot prove an expectation: {HAR_SOURCED}"
    assert verdict.tamper_detection is not None
    assert verdict.tamper_detection.level is TamperLevel.PARTIAL
    assert verdict.tamper_detection.reason.startswith(
        "the merged HAR added 1 request the trace never recorded; "
    )
    assert any(
        w.startswith("tamper detection is partial: the merged HAR") for w in verdict.warnings
    )


def _confirm_order(entries: list[dict[str, Any]]) -> None:
    order = next(e for e in entries if e["request"]["url"].endswith("/api/orders"))
    order["response"]["content"]["text"] = '{"order_id": 1001, "status": "confirmed"}'


async def test_recorded_trace_with_a_har_that_fills_the_order_response(tmp_path: Path) -> None:
    folder = RUNS / "honest_success"
    har = _har_with("honest_success", tmp_path, _confirm_order)
    run = load_run(trace=folder / "trace.zip", har=har)
    [order] = [n for n in run.network if n.url.endswith("/api/orders")]
    assert order.har_filled == {"response_body"}
    spec = TaskSpec.load(folder / "spec.yaml")
    confirmed = spec.expect[0].all[0].model_copy(update={"response_contains": ["confirmed"]})
    spec = spec.model_copy(
        update={
            "expect": [spec.expect[0].model_copy(update={"all": [confirmed]}), *spec.expect[1:]]
        }
    )

    verdict = await _judge(run, spec)

    assert verdict.outcome is Outcome.NEEDS_REVIEW
    result = _result(verdict, "order_submitted")
    assert result.met is None
    assert HAR_SOURCED in result.reason


@pytest.mark.parametrize("scenario", sorted(p.name for p in RUNS.iterdir() if p.is_dir()))
def test_recorded_hars_add_nothing_to_their_traces(scenario: str) -> None:
    folder = RUNS / scenario
    trace = load_playwright_trace(folder / "trace.zip")
    run = load_run(trace=folder / "trace.zip", har=folder / "run.har")

    assert har_contribution(run) == (0, 0)
    assert trace_witnessed(run) is run
    assert tamper_detection(run).level is tamper_detection(trace).level
