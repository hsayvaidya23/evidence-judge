from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evidence_judge.bench import (
    BenchConfig,
    BenchInputError,
    Scenario,
    llm_calls_upper_bound,
    load_scenario,
    load_scenarios,
)
from evidence_judge.core.verdict import Outcome
from tests.unit.conftest import forged_run

RUNS = Path(__file__).parents[1] / "fixtures" / "runs"


def test_loads_every_labelled_fixture() -> None:
    scenarios = load_scenarios(RUNS)
    by_name = {s.name: s for s in scenarios}
    assert sorted(by_name) == sorted(p.name for p in RUNS.iterdir())
    honest = by_name["honest_success"]
    assert honest.expected is Outcome.PASS
    assert honest.spec is not None
    assert honest.run.run_id == "honest_success"
    assert honest.run.task == "Buy a red kettle under $30"
    assert honest.run.agent_answer
    assert honest.run.network


def test_forgery_bases_are_failed_and_null_runs() -> None:
    run = forged_run()
    assert Scenario("a", run, Outcome.FAIL).forgery_base
    assert not Scenario("a", run, Outcome.PASS).forgery_base
    assert Scenario("null_agent", run, Outcome.NEEDS_REVIEW).forgery_base


def test_directories_without_labels_are_skipped(tmp_path: Path) -> None:
    shutil.copytree(RUNS / "null_agent", tmp_path / "null_agent")
    (tmp_path / "notes").mkdir()
    assert [s.name for s in load_scenarios(tmp_path)] == ["null_agent"]


def test_spec_is_optional(tmp_path: Path) -> None:
    folder = tmp_path / "bare"
    shutil.copytree(RUNS / "null_agent", folder)
    (folder / "spec.yaml").unlink()
    scenario = load_scenario(folder)
    assert scenario.spec is None
    assert scenario.run.task is None


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda d: (d / "trace.zip").unlink() or (d / "run.har").unlink(), "no trace.zip"),
        (lambda d: (d / "expected.json").write_text('{"outcome": "maybe"}'), "ValueError"),
        (lambda d: (d / "expected.json").write_text("{}"), "KeyError"),
        (lambda d: (d / "spec.yaml").write_text("id: x\n"), "ValidationError"),
        (lambda d: (d / "trace.zip").write_bytes(b"not a zip"), "scenario broken"),
    ],
)
def test_bad_scenarios_raise_input_errors(tmp_path: Path, setup: object, message: str) -> None:
    folder = tmp_path / "broken"
    shutil.copytree(RUNS / "null_agent", folder)
    assert callable(setup)
    setup(folder)
    with pytest.raises(BenchInputError, match=message):
        load_scenarios(tmp_path)


def test_empty_or_missing_directories_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(BenchInputError, match="no scenario directories"):
        load_scenarios(tmp_path)
    with pytest.raises(BenchInputError, match="not a directory"):
        load_scenarios(tmp_path / "missing")


def test_llm_call_bound_counts_only_llm_judgements() -> None:
    scenarios = load_scenarios(RUNS)
    offline = BenchConfig(seeds=2)
    assert llm_calls_upper_bound(scenarios, offline, baseline=False) == 0
    forged = 5 * 33 + 1 * 30
    cases = 9 + 3 * 3 + forged * 2
    assert llm_calls_upper_bound(scenarios, offline, baseline=True) == cases
    online = BenchConfig(seeds=2, offline=False)
    assert llm_calls_upper_bound(scenarios, online, baseline=True) == 4 * cases
