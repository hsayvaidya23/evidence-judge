"""Vacuous predicates must not let a fabricated claim pass."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evidence_judge.checks.claims import judge_claim
from evidence_judge.checks.registry import default_registry
from evidence_judge.config import Settings
from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.predicates import (
    ActionPredicate,
    PageTextPredicate,
    RequestPredicate,
    ScreenshotPredicate,
    UrlPredicate,
    ValuePredicate,
    vacuity_reason,
)
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.ingest import load_run
from tests.unit.conftest import ctx, honest_run

RUNS = Path(__file__).parents[1] / "fixtures" / "runs"


@pytest.mark.parametrize(
    "build",
    [
        lambda: RequestPredicate(),
        lambda: RequestPredicate(method="  "),
        lambda: RequestPredicate(method="POST", url_contains=["/"]),
        lambda: RequestPredicate(method="POST", body_contains=[" "]),
        lambda: RequestPredicate(method="POST", url_regex=" "),
        lambda: PageTextPredicate(contains=""),
        lambda: PageTextPredicate(contains="   "),
        lambda: PageTextPredicate(contains="24"),
        lambda: PageTextPredicate(contains="!!!"),
        lambda: UrlPredicate(),
        lambda: UrlPredicate(contains=" /"),
        lambda: ActionPredicate(),
        lambda: ActionPredicate(target_contains="a"),
        lambda: ValuePredicate(field="price", op="==", value=24),
        lambda: ValuePredicate(field="x", json_path="x", op="contains", value="a"),
        lambda: ValuePredicate(field=" ", json_path="x", value=1),
        lambda: ScreenshotPredicate(question=" "),
    ],
)
def test_vacuous_predicates_are_rejected(build: object) -> None:
    with pytest.raises(ValidationError):
        build()  # type: ignore[operator]


def test_strings_are_stripped() -> None:
    pred = RequestPredicate(method=" POST ", url_contains=[" /api/orders "])
    assert pred.method == "POST"
    assert pred.url_contains == ["/api/orders"]
    assert PageTextPredicate(contains=" Thank you ").contains == "Thank you"


@pytest.mark.parametrize(
    "pred",
    [
        RequestPredicate(method="POST"),
        RequestPredicate(url_contains=["/api/orders"]),
        RequestPredicate(response_contains=["1042"]),
        ActionPredicate(name="click"),
        PageTextPredicate(contains="confirm"),
        PageTextPredicate(contains="Order"),
    ],
)
def test_predicates_too_weak_to_prove_a_claim(pred: object) -> None:
    assert vacuity_reason(pred) is not None  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="too weak"):
        Claim(id="c", text="t", quote="q", kind=ClaimKind.OUTCOME, predicates=[pred])  # type: ignore[list-item]


@pytest.mark.parametrize(
    "pred",
    [
        RequestPredicate(method="POST", url_contains=["/api/orders"]),
        RequestPredicate(method="GET", response_contains=["1042"]),
        ActionPredicate(name="fill", value_contains="2"),
        PageTextPredicate(contains="1042"),
        PageTextPredicate(contains="$24.00"),
        PageTextPredicate(contains="Thank you for your order"),
        UrlPredicate(contains="/order/confirmation"),
    ],
)
def test_specific_predicates_prove_claims(pred: object) -> None:
    assert vacuity_reason(pred) is None  # type: ignore[arg-type]


async def test_constructed_vacuous_claim_is_never_supported() -> None:
    weak = RequestPredicate.model_construct(method="POST")
    claim = Claim.model_construct(
        id="c", text="placed", quote="placed", kind=ClaimKind.OUTCOME, key=True, predicates=[weak]
    )
    verdict = await judge_claim(claim, ctx(honest_run()), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert "cannot prove a claim" in verdict.reason


def test_spec_expect_needs_a_located_request_but_forbid_may_be_broad() -> None:
    with pytest.raises(ValidationError, match="expect 'order': a request predicate needs a method"):
        TaskSpec.model_validate(
            {
                "id": "s",
                "task": "t",
                "expect": [{"id": "order", "all": [{"kind": "request", "method": "POST"}]}],
            }
        )
    spec = TaskSpec.model_validate(
        {
            "id": "s",
            "task": "t",
            "forbid": [{"id": "no_writes", "any": [{"kind": "request", "method": "POST"}]}],
        }
    )
    assert spec.forbid[0].any


@pytest.mark.parametrize(
    "predicate",
    [
        {"kind": "request"},
        {"kind": "page_text", "contains": ""},
        {"kind": "page_text", "contains": " "},
    ],
)
def test_pre_extracted_vacuous_claims_are_refused(predicate: dict[str, object]) -> None:
    claims = {
        "claims": [
            {
                "id": "c1",
                "text": "The order was placed",
                "quote": "placed",
                "kind": "outcome",
                "key": True,
                "predicates": [predicate],
            }
        ]
    }
    with pytest.raises(ValidationError):
        ClaimSet.model_validate_json(json.dumps(claims))


async def test_forged_success_key_claims_are_not_supported_by_a_button_label() -> None:
    folder = RUNS / "forged_success"
    run = load_run(trace=folder / "trace.zip", answer_file=folder / "answer.txt")
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True)
    )
    assert verdict.outcome is Outcome.FAIL
    assert not [c for c in verdict.claims if c.key and c.status is Support.SUPPORTED]
    confirmation = next(c for c in verdict.claims if c.claim_text == "A confirmation was shown")
    assert "'1042'" in confirmation.reason
