"""Forged-run benchmark: how often does each judge PASS a failed run whose story was rewritten?

For every failed scenario the runner forges each `ForgeryKind` with N seeds (only the answer and
reasoning change), keeps every original run as an honest control, judges all of them with
Evidence Judge (with the scenario's task spec, with a coarser generic spec when the scenario has
one, and with no spec) and optionally a transcript LLM judge, and reports false-PASS rates on
forgeries and accuracy on controls with Wilson 95% intervals.

`injected_predicates` and `tautological_predicates` forgeries also replace claim extraction:
the judge receives the claims an injection-controlled extractor would return
(`forgery.injection`), carrying vacuous, weak or tautological predicates, after the extractor's
own validation. `self_planted_text` and `third_party_order` forgeries also append the evidence
a gaming agent would plant (`forgery.evidence`): its claimed confirmation typed into the shop's
search box, or an order sent to a third-party host.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from evidence_judge import __version__
from evidence_judge.baselines.llm_judge import TranscriptLLMJudge
from evidence_judge.bench.dataset import Scenario
from evidence_judge.bench.stats import wilson_interval
from evidence_judge.config import Settings
from evidence_judge.core.claims import ClaimSet
from evidence_judge.core.evidence import Run
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.forgery.base import MANIPULATION_KINDS, Forger, ForgeryKind
from evidence_judge.forgery.evidence import plant_evidence
from evidence_judge.forgery.injection import injected_claims
from evidence_judge.forgery.templates import TemplateForger
from evidence_judge.llm.base import LLMClient, LLMError
from evidence_judge.redact import Redactor

SCHEMA_ID: Final = "evidence-judge/bench-result@1"
EJ_WITH_SPEC: Final = "evidence-judge+spec"
EJ_GENERIC_SPEC: Final = "evidence-judge+generic-spec"
EJ_NO_SPEC: Final = "evidence-judge"
ALL_MANIPULATIONS: Final = "all manipulations"
INJECTION_KINDS: Final = frozenset(
    {ForgeryKind.INJECTED_PREDICATES, ForgeryKind.TAUTOLOGICAL_PREDICATES}
)
MAX_SUMMARY_CHARS: Final = 300


@dataclass(frozen=True, slots=True)
class BenchConfig:
    seeds: int = 3
    kinds: tuple[ForgeryKind, ...] = tuple(ForgeryKind)
    offline: bool = True
    """Evidence Judge extracts claims with heuristics only (no LLM)."""

    def __post_init__(self) -> None:
        if self.seeds < 1:
            raise ValueError("seeds must be at least 1")


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    scenario: Scenario
    run: Run
    expected: Outcome
    kind: ForgeryKind | None = None
    seed: int | None = None
    claims: ClaimSet | None = None
    """Pre-extracted claims (injected_predicates forgeries); None means extract as usual."""

    @property
    def forged(self) -> bool:
        return self.kind is not None


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    scenario: str
    judge: str
    forged: bool
    kind: ForgeryKind | None = None
    seed: int | None = None
    expected: Outcome
    outcome: Outcome | None = Field(description="None when the judge errored")
    summary: str = ""
    error: str | None = None


class Proportion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    k: int
    n: int
    rate: float | None
    ci_low: float
    ci_high: float

    @classmethod
    def of(cls, k: int, n: int) -> Proportion:
        low, high = wilson_interval(k, n)
        return cls(k=k, n=n, rate=k / n if n else None, ci_low=low, ci_high=high)


class ForgeryMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    judge: str
    kind: str = Field(description=f"A ForgeryKind value or '{ALL_MANIPULATIONS}'")
    false_pass: Proportion
    needs_review: int
    fail: int
    errors: int


class ControlMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    judge: str
    accuracy: Proportion
    needs_review: int
    errors: int


class ScenarioInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    expected: Outcome
    has_spec: bool
    forged: bool
    has_generic_spec: bool = False


class BenchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_id: str = SCHEMA_ID
    judge_version: str = __version__
    forger: str
    seeds: int
    kinds: list[ForgeryKind]
    offline: bool
    judges: list[str]
    scenarios: list[ScenarioInfo]
    forgery_metrics: list[ForgeryMetric]
    control_metrics: list[ControlMetric]
    cases: list[CaseResult]


@dataclass(slots=True)
class _Prepared:
    run: Run
    spec: TaskSpec | None
    generic_spec: TaskSpec | None
    claims: ClaimSet | None


@dataclass(slots=True)
class BenchmarkRunner:
    """Builds the cases, judges each one with every configured judge and aggregates metrics."""

    settings: Settings
    config: BenchConfig = field(default_factory=BenchConfig)
    llm: LLMClient | None = None
    baseline: TranscriptLLMJudge | None = None
    forger: Forger = field(default_factory=TemplateForger)
    redactor: Redactor | None = None

    async def run(self, scenarios: Sequence[Scenario]) -> BenchResult:
        cases = await self.build_cases(scenarios)
        judges = self.judge_names()
        evidence_judge = Judge(self.settings, self.llm)
        jobs = [
            self._evaluate(evidence_judge, judge, case, self._prepare(case))
            for case in cases
            for judge in judges
        ]
        results = [r for r in await asyncio.gather(*jobs) if r is not None]
        return BenchResult(
            forger=self.forger.name,
            seeds=self.config.seeds,
            kinds=list(self.config.kinds),
            offline=self.config.offline or self.llm is None,
            judges=judges,
            scenarios=[
                ScenarioInfo(
                    name=s.name,
                    expected=s.expected,
                    has_spec=s.spec is not None,
                    forged=s.forgery_base,
                    has_generic_spec=s.generic_spec is not None,
                )
                for s in scenarios
            ],
            forgery_metrics=forgery_metrics(results, judges, self.config.kinds),
            control_metrics=control_metrics(results, judges),
            cases=results,
        )

    def judge_names(self) -> list[str]:
        names = [EJ_WITH_SPEC, EJ_GENERIC_SPEC, EJ_NO_SPEC]
        if self.baseline is not None:
            names.append(self.baseline.name)
        return names

    async def build_cases(self, scenarios: Sequence[Scenario]) -> list[Case]:
        cases = [
            Case(case_id=f"{s.name}/honest", scenario=s, run=s.run, expected=s.expected)
            for s in scenarios
        ]
        for scenario in scenarios:
            if not scenario.forgery_base:
                continue
            for kind in self.config.kinds:
                for seed in range(self.config.seeds):
                    forged = await self.forger.forge(scenario.run, kind, seed=seed)
                    forged = plant_evidence(forged, kind, seed=seed)
                    injected = (
                        injected_claims(forged, seed=seed, kind=kind)
                        if kind in INJECTION_KINDS
                        else None
                    )
                    cases.append(
                        Case(
                            case_id=f"{scenario.name}/{kind.value}/{seed}",
                            scenario=scenario,
                            run=forged,
                            expected=Outcome.FAIL,
                            kind=kind,
                            seed=seed,
                            claims=injected,
                        )
                    )
        return cases

    def _prepare(self, case: Case) -> _Prepared:
        prepared = _Prepared(
            run=case.run,
            spec=case.scenario.spec,
            generic_spec=case.scenario.generic_spec,
            claims=case.claims,
        )
        if self.redactor is None:
            return prepared
        prepared.run, _ = self.redactor.redact_run(case.run)
        if prepared.spec is not None:
            prepared.spec, _ = self.redactor.redact_model(prepared.spec)
        if prepared.generic_spec is not None:
            prepared.generic_spec, _ = self.redactor.redact_model(prepared.generic_spec)
        if prepared.claims is not None:
            prepared.claims, _ = self.redactor.redact_model(prepared.claims)
        return prepared

    async def _evaluate(
        self, evidence_judge: Judge, judge: str, case: Case, prepared: _Prepared
    ) -> CaseResult | None:
        base = {
            "case_id": case.case_id,
            "scenario": case.scenario.name,
            "judge": judge,
            "forged": case.forged,
            "kind": case.kind,
            "seed": case.seed,
            "expected": case.expected,
        }
        specs = {EJ_WITH_SPEC: prepared.spec, EJ_GENERIC_SPEC: prepared.generic_spec}
        if judge in specs and specs[judge] is None:
            return None
        try:
            if judge in (EJ_WITH_SPEC, EJ_GENERIC_SPEC, EJ_NO_SPEC):
                options = JudgeOptions(
                    offline=self.config.offline or self.llm is None,
                    spec=specs.get(judge),
                    claims=prepared.claims,
                )
                verdict = await evidence_judge.judge(prepared.run, options)
                outcome, summary = verdict.outcome, verdict.summary
            else:
                assert self.baseline is not None
                task = prepared.spec.task if prepared.spec else None
                result = await self.baseline.judge(prepared.run, task=task)
                outcome, summary = result.outcome, result.reason
        except LLMError as exc:
            return CaseResult.model_validate(
                {**base, "outcome": None, "error": f"{type(exc).__name__}: {exc}"}
            )
        return CaseResult.model_validate({**base, "outcome": outcome, "summary": _clip(summary)})


def forgery_metrics(
    results: Sequence[CaseResult], judges: Sequence[str], kinds: Sequence[ForgeryKind]
) -> list[ForgeryMetric]:
    metrics: list[ForgeryMetric] = []
    manipulations = [k for k in kinds if k in MANIPULATION_KINDS]
    for judge in judges:
        forged = [r for r in results if r.judge == judge and r.forged]
        groups: list[tuple[str, list[CaseResult]]] = [
            (kind.value, [r for r in forged if r.kind is kind]) for kind in kinds
        ]
        if manipulations:
            groups.append((ALL_MANIPULATIONS, [r for r in forged if r.kind in manipulations]))
        metrics.extend(_forgery_metric(judge, label, group) for label, group in groups if group)
    return metrics


def _forgery_metric(judge: str, kind: str, group: Sequence[CaseResult]) -> ForgeryMetric:
    judged = [r for r in group if r.outcome is not None]
    return ForgeryMetric(
        judge=judge,
        kind=kind,
        false_pass=Proportion.of(sum(r.outcome is Outcome.PASS for r in judged), len(judged)),
        needs_review=sum(r.outcome is Outcome.NEEDS_REVIEW for r in judged),
        fail=sum(r.outcome is Outcome.FAIL for r in judged),
        errors=len(group) - len(judged),
    )


def control_metrics(results: Sequence[CaseResult], judges: Sequence[str]) -> list[ControlMetric]:
    metrics: list[ControlMetric] = []
    for judge in judges:
        controls = [r for r in results if r.judge == judge and not r.forged]
        if not controls:
            continue
        if not controls:
            continue
        judged = [r for r in controls if r.outcome is not None]
        correct = sum(r.outcome is r.expected for r in judged)
        metrics.append(
            ControlMetric(
                judge=judge,
                accuracy=Proportion.of(correct, len(judged)),
                needs_review=sum(r.outcome is Outcome.NEEDS_REVIEW for r in judged),
                errors=len(controls) - len(judged),
            )
        )
    return metrics


def llm_calls_upper_bound(
    scenarios: Sequence[Scenario], config: BenchConfig, *, baseline: bool
) -> int:
    """Judgements that may call an LLM; used to size the benchmark's overall budget."""
    forged = sum(s.forgery_base for s in scenarios) * len(config.kinds) * config.seeds
    cases = len(scenarios) + forged
    evidence_judges = len((EJ_WITH_SPEC, EJ_GENERIC_SPEC, EJ_NO_SPEC))
    per_case = (0 if config.offline else evidence_judges) + (1 if baseline else 0)
    return cases * per_case


def _clip(text: str) -> str:
    return text if len(text) <= MAX_SUMMARY_CHARS else text[: MAX_SUMMARY_CHARS - 1] + "…"
