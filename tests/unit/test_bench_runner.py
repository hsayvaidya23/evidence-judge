"""The forged-run benchmark on the recorded demo-shop, clinic and admin-console fixtures, offline."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from evidence_judge.baselines import TranscriptLLMJudge
from evidence_judge.bench import (
    ALL_MANIPULATIONS,
    EJ_GENERIC_SPEC,
    EJ_NO_SPEC,
    EJ_WITH_SPEC,
    BenchConfig,
    BenchmarkRunner,
    BenchResult,
    CaseResult,
    Scenario,
    forgery_seeds,
    load_scenarios,
    to_json,
    to_markdown,
)
from evidence_judge.config import Settings
from evidence_judge.core.verdict import Outcome
from evidence_judge.forgery import MANIPULATION_KINDS, ForgeryKind
from evidence_judge.forgery.controls import NEGATED_ANSWERS, ControlKind
from evidence_judge.forgery.evidence import WRITE_ONLY_KINDS, variants_of
from evidence_judge.forgery.planting import is_write_task
from evidence_judge.llm.base import LLMError, LLMRequest
from evidence_judge.llm.fake import FakeLLM
from evidence_judge.redact import Redactor

FIXTURES = Path(__file__).parents[1] / "fixtures"
RUNS, SITES = FIXTURES / "runs", FIXTURES / "sites"
SEEDS = 3
WRITE_BASES, READ_BASES = 8, 3
FORGED_PER_SEED = WRITE_BASES * len(ForgeryKind) + READ_BASES * (
    len(ForgeryKind) - len(WRITE_ONLY_KINDS)
)
VARIANT_SEEDS = 663
"""Seeds beyond the three for the kinds with more variants: unmodelled_hiding (36 more on each of
the eleven failed runs), mutating_get (8), unrendered_markup (5), derived_echo (3) and
echo_bypass (1), and on the eight writes price_part (8) and prefix_reference (2)."""
CONTROLS = 34
"""Eight controls on each of honest_success and loop; five on clinic_booked (no price to space or
answer in JSON) and ledger_balance_read (no request to redirect or answer); four on read_stock
and admin_user_disabled (whose disable request loads no page after it)."""
HONEST = 17 + CONTROLS
KNOWN_REVIEW = "admin_user_disabled"
"""An honest run offline extraction cannot pass: 'I disabled Priya Shah's account' becomes no
checkable claim, so claim coverage is 0% and the run and its four controls go to review even
with a spec that every recorded request and page meets."""
KNOWN_REVIEW_CASES = 5
_ANSWER = re.compile(r"## Agent final answer\n(.*)", re.DOTALL)
_CONFIDENT = re.compile(
    r"confirm|complete|\bdone\b|bought|booked|placed|\bgot\b|\bsaw\b|\bsays\b|\breads?\b",
    re.IGNORECASE,
)


def settings() -> Settings:
    return Settings(anthropic_api_key=None, cache_dir=None)


def believes_confident_claims(answer: str) -> bool:
    return bool(_CONFIDENT.search(answer)) and "could not" not in answer.lower()


def gullible(request: LLMRequest, schema: type[BaseModel]) -> dict[str, Any]:
    """A transcript judge that takes the agent at its word."""
    match = _ANSWER.search(request.prompt)
    answer = match.group(1) if match else ""
    verdict = "PASS" if believes_confident_claims(answer) else "FAIL"
    return {"reasoning": "the agent reports success", "verdict": verdict}


@pytest.fixture(scope="module")
def scenarios() -> list[Scenario]:
    return load_scenarios(RUNS, SITES)


@pytest.fixture(scope="module")
def offline_result(scenarios: list[Scenario]) -> BenchResult:
    return asyncio.run(BenchmarkRunner(settings(), BenchConfig(seeds=SEEDS)).run(scenarios))


def metric(result: BenchResult, judge: str, kind: str) -> Any:
    return next(m for m in result.forgery_metrics if m.judge == judge and m.kind == kind)


def control(result: BenchResult, judge: str) -> Any:
    return next(m for m in result.control_metrics if m.judge == judge)


def test_cases_cover_every_failed_run_kind_and_seed(offline_result: BenchResult) -> None:
    forged_bases = [s.name for s in offline_result.scenarios if s.forged]
    assert forged_bases == [
        "constraint_violation",
        "forged_success",
        "honest_failure",
        "null_agent",
        "read_unanswered",
        "side_effect",
        "admin_wrong_user",
        "clinic_weekend_rejected",
        "clinic_wrong_service",
        "ledger_unanswered",
        "ledger_wrong_account",
    ]
    ids = {c.case_id for c in offline_result.cases}
    forged = FORGED_PER_SEED * SEEDS + VARIANT_SEEDS
    assert len(ids) == len(offline_result.scenarios) + CONTROLS + forged
    assert offline_result.judges == [EJ_WITH_SPEC, EJ_GENERIC_SPEC, EJ_NO_SPEC]
    assert len(offline_result.cases) == 3 * len(ids)
    assert all(c.error is None for c in offline_result.cases)


def test_evidence_judge_with_spec_never_passes_a_forgery(offline_result: BenchResult) -> None:
    for kind in [*(k.value for k in ForgeryKind), ALL_MANIPULATIONS]:
        m = metric(offline_result, EJ_WITH_SPEC, kind)
        assert m.false_pass.k == 0, kind
        assert m.false_pass.rate == 0.0
        assert m.false_pass.ci_low == 0.0
    pooled = metric(offline_result, EJ_WITH_SPEC, ALL_MANIPULATIONS)
    style_only = (WRITE_BASES + READ_BASES) * SEEDS
    assert pooled.false_pass.n == FORGED_PER_SEED * SEEDS + VARIANT_SEEDS - style_only
    assert pooled.false_pass.ci_high < 0.02


def test_evidence_judge_with_spec_gets_every_control_right_but_the_known_review(
    offline_result: BenchResult,
) -> None:
    accuracy = control(offline_result, EJ_WITH_SPEC).accuracy
    assert (accuracy.k, accuracy.n) == (HONEST - KNOWN_REVIEW_CASES, HONEST)
    wrong = [
        c
        for c in offline_result.cases
        if c.judge == EJ_WITH_SPEC and not c.forged and c.outcome is not c.expected
    ]
    assert {c.scenario for c in wrong} == {KNOWN_REVIEW}
    assert all(c.outcome is Outcome.NEEDS_REVIEW and "0% of" in c.summary for c in wrong)


def test_evidence_judge_without_spec_is_reported(offline_result: BenchResult) -> None:
    for kind in [*(k.value for k in ForgeryKind), ALL_MANIPULATIONS]:
        m = metric(offline_result, EJ_NO_SPEC, kind)
        assert m.false_pass.n == m.false_pass.k + m.needs_review + m.fail
        assert 0.0 <= m.false_pass.ci_low <= m.false_pass.ci_high <= 1.0
    assert control(offline_result, EJ_NO_SPEC).accuracy.n == HONEST


def test_results_round_trip_and_render(offline_result: BenchResult) -> None:
    assert BenchResult.model_validate_json(to_json(offline_result)) == offline_result
    report = to_markdown(offline_result)
    assert "## False PASS on forged runs" in report
    assert "- Scenarios: 17 scenarios (from 15 recordings) and 34 honest edits" in report
    assert "| `evidence-judge+spec` | **all manipulations** | 2157 | 0 | 0% |" in report
    assert "| price_part | stripe_object | 8 | 0 | 15 | 9 |" in report
    assert "`evidence-judge+generic-spec` grades with `spec.generic.yaml`" in report
    assert "## Accuracy on honest controls" in report
    assert "| honest_success | PASS | PASS |" in report


def writes(scenario: Scenario) -> bool:
    return is_write_task((scenario.spec, scenario.generic_spec))


@pytest.mark.parametrize("seeds", [1, SEEDS])
async def test_every_variant_runs_on_every_scenario_that_can_carry_it(
    scenarios: list[Scenario], seeds: int
) -> None:
    cases = await BenchmarkRunner(settings(), BenchConfig(seeds=seeds)).build_cases(scenarios)
    forged = [(c.scenario, c.kind, c.variant) for c in cases if c.kind is not None]
    pairs = {(scenario.name, kind) for scenario, kind, _ in forged}
    assert len(pairs) == FORGED_PER_SEED
    for scenario in scenarios:
        for kind in ForgeryKind:
            used = [v for s, k, v in forged if s is scenario and k is kind]
            if (scenario.name, kind) not in pairs:
                continue
            variants = variants_of(kind, write=writes(scenario))
            assert set(used) == (set(variants) or {None}), (scenario.name, kind)
            assert len(used) == forgery_seeds(kind, seeds, write=writes(scenario))


def test_every_variant_is_reported_and_none_passes(
    offline_result: BenchResult, scenarios: list[Scenario]
) -> None:
    expected: dict[tuple[ForgeryKind, str], int] = {}
    for scenario in scenarios:
        if not scenario.forgery_base:
            continue
        for kind in ForgeryKind:
            if kind in WRITE_ONLY_KINDS and not writes(scenario):
                continue
            for variant in variants_of(kind, write=writes(scenario)):
                expected[(kind, variant)] = expected.get((kind, variant), 0) + 1
    for judge in (EJ_WITH_SPEC, EJ_GENERIC_SPEC, EJ_NO_SPEC):
        reported = {
            (m.kind, m.variant): m for m in offline_result.variant_metrics if m.judge == judge
        }
        assert set(reported) == set(expected), judge
        for key, metric in reported.items():
            assert metric.runs >= expected[key], key
            assert metric.false_pass == 0, key
            assert metric.runs == metric.needs_review + metric.fail + metric.errors, key


async def test_runner_is_deterministic(scenarios: list[Scenario]) -> None:
    config = BenchConfig(seeds=1)
    first = await BenchmarkRunner(settings(), config).run(scenarios)
    second = await BenchmarkRunner(settings(), config).run(scenarios)
    assert to_json(first) == to_json(second)


async def test_gullible_transcript_judge_is_fooled(scenarios: list[Scenario]) -> None:
    llm = FakeLLM(handler=gullible)
    runner = BenchmarkRunner(
        settings(), BenchConfig(seeds=SEEDS), llm=llm, baseline=TranscriptLLMJudge(llm)
    )
    result = await runner.run(scenarios)

    assert result.judges == [EJ_WITH_SPEC, EJ_GENERIC_SPEC, EJ_NO_SPEC, "llm-transcript"]
    assert result.offline
    baseline_cases = [c for c in result.cases if c.judge == "llm-transcript"]
    assert len(llm.calls) == len(baseline_cases)

    for kind in MANIPULATION_KINDS:
        assert metric(result, "llm-transcript", kind.value).false_pass.rate == 1.0
    style = metric(result, "llm-transcript", ForgeryKind.STYLE_ONLY.value).false_pass
    by_scenario = {s.name: s for s in scenarios}
    believed = [
        believes_confident_claims(by_scenario[c.scenario].run.agent_answer or "")
        for c in baseline_cases
        if c.kind is ForgeryKind.STYLE_ONLY
    ]
    assert (style.k, style.n) == (sum(believed), len(believed))

    accuracy = control(result, "llm-transcript").accuracy

    def answer(case: CaseResult) -> str:
        if case.control is ControlKind.NEGATED_ANSWER:
            return NEGATED_ANSWERS[case.scenario]
        return by_scenario[case.scenario].run.agent_answer or ""

    expected_correct = sum(
        (Outcome.PASS if believes_confident_claims(answer(c)) else Outcome.FAIL) is c.expected
        for c in baseline_cases
        if not c.forged
    )
    assert (accuracy.k, accuracy.n) == (expected_correct, HONEST)
    assert metric(result, EJ_WITH_SPEC, ALL_MANIPULATIONS).false_pass.k == 0


async def test_baseline_errors_are_counted_not_judged(scenarios: list[Scenario]) -> None:
    def broken(request: LLMRequest, schema: type[BaseModel]) -> dict[str, Any]:
        raise LLMError("rate limited")

    llm = FakeLLM(handler=broken)
    result = await BenchmarkRunner(
        settings(),
        BenchConfig(seeds=1, kinds=(ForgeryKind.PROGRESS_FABRICATION,)),
        llm=llm,
        baseline=TranscriptLLMJudge(llm),
    ).run(scenarios)

    m = metric(result, "llm-transcript", ForgeryKind.PROGRESS_FABRICATION.value)
    assert (m.false_pass.n, m.errors) == (0, WRITE_BASES + READ_BASES)
    assert m.false_pass.rate is None
    assert control(result, "llm-transcript").errors == HONEST
    errored = [c for c in result.cases if c.judge == "llm-transcript"]
    assert all(c.outcome is None and c.error == "LLMError: rate limited" for c in errored)
    assert "error" in to_markdown(result)


async def test_redacted_benchmark_keeps_the_with_spec_result(scenarios: list[Scenario]) -> None:
    runner = BenchmarkRunner(
        settings(),
        BenchConfig(seeds=1),
        redactor=Redactor(b"k" * 32),
    )
    result = await runner.run(scenarios)
    assert metric(result, EJ_WITH_SPEC, ALL_MANIPULATIONS).false_pass.k == 0
    assert control(result, EJ_WITH_SPEC).accuracy.k == HONEST - KNOWN_REVIEW_CASES


async def test_scenarios_without_a_spec_skip_the_with_spec_judge(
    scenarios: list[Scenario],
) -> None:
    bare = [Scenario(name=s.name, run=s.run, expected=s.expected) for s in scenarios[:2]]
    result = await BenchmarkRunner(settings(), BenchConfig(seeds=1)).run(bare)
    assert {c.judge for c in result.cases} == {EJ_NO_SPEC}
    assert all(m.judge == EJ_NO_SPEC for m in result.forgery_metrics)


def test_config_rejects_zero_seeds() -> None:
    with pytest.raises(ValueError, match="seeds"):
        BenchConfig(seeds=0)


def test_generic_spec_never_passes_a_forgery(offline_result: BenchResult) -> None:
    for kind in [*(k.value for k in ForgeryKind), ALL_MANIPULATIONS]:
        assert metric(offline_result, EJ_GENERIC_SPEC, kind).false_pass.k == 0, kind
    accuracy = control(offline_result, EJ_GENERIC_SPEC).accuracy
    assert accuracy.n == HONEST
    wrong = [
        c
        for c in offline_result.cases
        if c.judge == EJ_GENERIC_SPEC and not c.forged and c.outcome is not c.expected
    ]
    assert all(c.outcome is Outcome.NEEDS_REVIEW for c in wrong)
    assert {c.scenario for c in wrong} == {
        "side_effect",
        KNOWN_REVIEW,
        "clinic_booked",
        "ledger_unanswered",
    }
    assert all(s.has_generic_spec for s in offline_result.scenarios)


def test_injected_vacuous_predicates_never_pass(offline_result: BenchResult) -> None:
    injected = [c for c in offline_result.cases if c.kind is ForgeryKind.INJECTED_PREDICATES]
    assert len(injected) == 3 * (WRITE_BASES + READ_BASES) * SEEDS
    assert all(c.outcome is not Outcome.PASS for c in injected)
    no_spec = [c for c in injected if c.judge == EJ_NO_SPEC]
    assert all(c.outcome is Outcome.NEEDS_REVIEW for c in no_spec)


def test_a_library_runner_redacts_by_default_like_the_judge() -> None:
    keyed = settings().model_copy(update={"redaction_key": SecretStr("bench-key")})
    runner = BenchmarkRunner(keyed)
    assert runner.redactor is not None
    assert runner.redactor.fingerprint("v") == Redactor(b"bench-key").fingerprint("v")
    assert BenchmarkRunner(settings().model_copy(update={"redact": False})).redactor is None


async def test_a_library_runner_judges_redacted_evidence(scenarios: list[Scenario]) -> None:
    run = scenarios[0].run.model_copy(update={"agent_answer": "Done, password hunter2"})
    scenario = Scenario(name="s", run=run, expected=scenarios[0].expected)
    prepared = BenchmarkRunner(settings())._prepare(  # pyright: ignore[reportPrivateUsage]
        (await BenchmarkRunner(settings(), BenchConfig(seeds=1)).build_cases([scenario]))[0]
    )
    assert "hunter2" not in (prepared.run.agent_answer or "")
