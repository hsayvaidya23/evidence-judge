from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelName
from inspect_ai.scorer import Target
from inspect_ai.solver import TaskState

from evidence_judge.config import Settings
from evidence_judge.core.evidence import Run
from evidence_judge.core.verdict import Outcome, RunVerdict
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.integrations.inspect_ai import evidence_judge_scorer, outcome_value
from tests.unit.test_report_fixtures import make_verdict

RUNS = Path(__file__).parents[1] / "fixtures" / "runs"
OFFLINE = Settings(anthropic_api_key=None, cache_dir=None)


def state(metadata: dict[str, Any], completion: str = "") -> TaskState:
    task_state = TaskState(
        model=ModelName("mockllm/model"),
        sample_id=1,
        epoch=1,
        input="Buy a red kettle under $30",
        messages=[],
        metadata=metadata,
    )
    task_state.output.completion = completion
    return task_state


class StubJudge(Judge):
    def __init__(self, verdict: RunVerdict) -> None:
        super().__init__(OFFLINE)
        self.verdict = verdict
        self.runs: list[Run] = []
        self.options: list[JudgeOptions | None] = []

    async def judge(self, run: Run, options: JudgeOptions | None = None) -> RunVerdict:
        self.runs.append(run)
        self.options.append(options)
        return self.verdict


def test_outcome_values() -> None:
    assert outcome_value(Outcome.PASS) == "C"
    assert outcome_value(Outcome.FAIL) == "I"
    assert outcome_value(Outcome.NEEDS_REVIEW) == "N"
    assert outcome_value(Outcome.NEEDS_REVIEW, "P") == "P"


async def test_real_offline_judge_fails_the_forged_run() -> None:
    folder = RUNS / "forged_success"
    score = evidence_judge_scorer(offline=True, settings=OFFLINE, base_dir=folder)
    result = await score(
        state(
            {
                "trace": "trace.zip",
                "har": "run.har",
                "answer_file": "answer.txt",
                "reasoning_file": "reasoning.txt",
                "spec": "spec.yaml",
            }
        ),
        Target(""),
    )

    assert result is not None
    assert result.value == "I"
    assert result.metadata is not None
    assert result.metadata["outcome"] == "fail"
    assert result.metadata["schema_id"] == "evidence-judge/run-verdict@1"
    assert result.explanation == result.metadata["summary"]
    assert result.answer == (folder / "answer.txt").read_text().strip()


async def test_stub_judge_gets_the_completion_as_answer_and_numeric_value() -> None:
    folder = RUNS / "forged_success"
    stub = StubJudge(make_verdict(Outcome.NEEDS_REVIEW))
    score = evidence_judge_scorer(
        value="score", settings=OFFLINE, judge=stub, redact=False, offline=True
    )
    result = await score(
        state({"trace": str(folder / "trace.zip"), "run_id": "sample-1"}, "Order 1042 placed"),
        Target(""),
    )

    assert result is not None
    assert result.value == 0.42
    [run] = stub.runs
    assert run.agent_answer == "Order 1042 placed"
    assert run.task == "Buy a red kettle under $30"
    assert run.run_id == "sample-1"


async def test_needs_review_maps_to_the_configured_grade() -> None:
    folder = RUNS / "forged_success"
    stub = StubJudge(make_verdict(Outcome.NEEDS_REVIEW))
    score = evidence_judge_scorer(needs_review="P", settings=OFFLINE, judge=stub, base_dir=folder)
    result = await score(state({"trace": "trace.zip"}), Target(""))
    assert result is not None and result.value == "P"


async def test_sample_without_evidence_is_an_error() -> None:
    score = evidence_judge_scorer(settings=OFFLINE, judge=StubJudge(make_verdict()))
    with pytest.raises(ValueError, match="names no evidence"):
        await score(state({"spec": "spec.yaml"}), Target(""))


def test_runs_inside_an_inspect_eval(tmp_path: Path) -> None:
    folders = [RUNS / "forged_success", RUNS / "honest_success"]
    task = Task(
        dataset=[
            Sample(
                id=folder.name,
                input="Buy a red kettle under $30",
                metadata={
                    "trace": str(folder / "trace.zip"),
                    "har": str(folder / "run.har"),
                    "answer_file": str(folder / "answer.txt"),
                    "spec": str(folder / "spec.yaml"),
                },
            )
            for folder in folders
        ],
        scorer=evidence_judge_scorer(offline=True, settings=OFFLINE),
    )

    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")

    assert log.status == "success", log.error
    assert log.samples is not None
    grades = {
        str(s.id): s.scores["evidence_judge"].value for s in log.samples if s.scores is not None
    }
    assert grades == {"forged_success": "I", "honest_success": "C"}
    assert log.results is not None
    [scores] = log.results.scores
    assert scores.metrics["accuracy"].value == 0.5


async def test_offline_scorer_does_not_create_a_redaction_key(tmp_path: Path) -> None:
    settings = Settings(anthropic_api_key=None, cache_dir=tmp_path / "cache", redaction_key=None)
    folder = RUNS / "forged_success"
    score = evidence_judge_scorer(offline=True, settings=settings, base_dir=folder)
    await score(state({"trace": "trace.zip", "answer_file": "answer.txt"}), Target(""))
    assert not (tmp_path / "cache").exists()
