"""Forged-run benchmark: false-PASS rates of judges on runs whose story was rewritten."""

from evidence_judge.bench.dataset import BenchInputError, Scenario, load_scenario, load_scenarios
from evidence_judge.bench.report import to_json, to_markdown, write_json, write_markdown
from evidence_judge.bench.runner import (
    ALL_MANIPULATIONS,
    EJ_NO_SPEC,
    EJ_WITH_SPEC,
    BenchConfig,
    BenchmarkRunner,
    BenchResult,
    Case,
    CaseResult,
    ControlMetric,
    ForgeryMetric,
    Proportion,
    control_metrics,
    forgery_metrics,
    llm_calls_upper_bound,
)
from evidence_judge.bench.stats import Z_95, wilson_interval

__all__ = [
    "ALL_MANIPULATIONS",
    "EJ_NO_SPEC",
    "EJ_WITH_SPEC",
    "Z_95",
    "BenchConfig",
    "BenchInputError",
    "BenchResult",
    "BenchmarkRunner",
    "Case",
    "CaseResult",
    "ControlMetric",
    "ForgeryMetric",
    "Proportion",
    "Scenario",
    "control_metrics",
    "forgery_metrics",
    "llm_calls_upper_bound",
    "load_scenario",
    "load_scenarios",
    "to_json",
    "to_markdown",
    "wilson_interval",
    "write_json",
    "write_markdown",
]
