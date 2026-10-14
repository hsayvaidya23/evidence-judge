"""Golden regression: every recorded demo-shop run must get its expected verdict, offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_judge.config import Settings
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.ingest import load_run

RUNS = Path(__file__).parents[1] / "fixtures" / "runs"
SCENARIOS = sorted(p.name for p in RUNS.iterdir() if (p / "expected.json").exists())


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_recorded_run_gets_expected_outcome(scenario: str) -> None:
    folder = RUNS / scenario
    run = load_run(
        trace=folder / "trace.zip",
        har=folder / "run.har",
        answer_file=folder / "answer.txt",
        reasoning_file=folder / "reasoning.txt",
    )
    spec = TaskSpec.load(folder / "spec.yaml")
    expected = Outcome(json.loads((folder / "expected.json").read_text())["outcome"])

    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, spec=spec)
    )

    assert verdict.outcome is expected, verdict.summary
    for claim in verdict.claims:
        if claim.status is Support.SUPPORTED:
            assert claim.citations, f"{claim.claim_id} is supported without citing evidence"


async def test_forged_order_number_is_contradicted() -> None:
    folder = RUNS / "forged_success"
    run = load_run(trace=folder / "trace.zip", answer_file=folder / "answer.txt")
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True)
    )
    assert verdict.outcome is Outcome.FAIL
    assert any(c.status is Support.CONTRADICTED and c.key for c in verdict.claims)
