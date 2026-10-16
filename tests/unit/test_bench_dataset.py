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
from evidence_judge.bench.dataset import redaction_key
from evidence_judge.core.verdict import Outcome
from tests.unit.conftest import forged_run

RUNS = Path(__file__).parents[1] / "fixtures" / "runs"
SITES = Path(__file__).parents[1] / "fixtures" / "sites"


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


def test_loads_several_directories_in_order() -> None:
    names = [s.name for s in load_scenarios(RUNS, SITES)]
    assert names == sorted(p.name for p in RUNS.iterdir()) + sorted(p.name for p in SITES.iterdir())
    by_name = {s.name: s for s in load_scenarios(SITES)}
    assert by_name["clinic_booked"].run.task == "Book a dental check-up on 3 November 2026"
    assert by_name["ledger_balance_read"].spec is not None
    assert not by_name["ledger_balance_read"].spec.changes_state()


def test_scenarios_that_judge_one_recording_again_share_its_digest() -> None:
    by_name = {s.name: s for s in load_scenarios(RUNS)}
    assert by_name["read_stock"].recording == by_name["honest_failure"].recording
    assert by_name["read_unanswered"].recording == by_name["null_agent"].recording
    assert by_name["honest_success"].recording != by_name["honest_failure"].recording


def test_a_scenario_in_two_directories_is_rejected(tmp_path: Path) -> None:
    shutil.copytree(RUNS / "null_agent", tmp_path / "null_agent")
    with pytest.raises(BenchInputError, match="null_agent is in more than one directory"):
        load_scenarios(RUNS, tmp_path)


def test_redaction_key_depends_only_on_the_recorded_files(tmp_path: Path) -> None:
    key = redaction_key(RUNS, SITES)
    assert key == redaction_key(RUNS, SITES)
    assert key not in (redaction_key(RUNS), redaction_key(SITES, RUNS))
    shutil.copytree(SITES / "clinic_booked", tmp_path / "clinic_booked")
    copy = redaction_key(tmp_path)
    (tmp_path / "clinic_booked" / "answer.txt").write_text("edited", encoding="utf-8")
    assert redaction_key(tmp_path) != copy


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
    forged = 5 * 44 + 1 * 39
    cases = 9 + 3 * 4 + forged * 2
    assert llm_calls_upper_bound(scenarios, offline, baseline=True) == cases
    online = BenchConfig(seeds=2, offline=False)
    assert llm_calls_upper_bound(scenarios, online, baseline=True) == 4 * cases
