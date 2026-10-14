from __future__ import annotations

import random
from pathlib import Path

import pytest

from evidence_judge.core.evidence import Run
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.extract import HeuristicExtractor
from evidence_judge.extract.text import split_sentences
from evidence_judge.forgery import (
    MANIPULATION_KINDS,
    ForgeryKind,
    TemplateForger,
    derive_facts,
    forge,
)
from evidence_judge.forgery.facts import Domain
from evidence_judge.ingest import load_run
from tests.unit.conftest import forged_run

RUNS = Path(__file__).parents[1] / "fixtures" / "runs"
EVIDENCE_FIELDS = {"agent_answer", "agent_reasoning", "metadata"}


def fixture_run(scenario: str) -> Run:
    folder = RUNS / scenario
    return load_run(
        trace=folder / "trace.zip",
        har=folder / "run.har",
        answer_file=folder / "answer.txt",
        reasoning_file=folder / "reasoning.txt",
        task=TaskSpec.load(folder / "spec.yaml").task,
        run_id=scenario,
    )


@pytest.fixture(scope="module")
def honest_failure() -> Run:
    return fixture_run("honest_failure")


@pytest.mark.parametrize("kind", list(ForgeryKind))
def test_same_seed_gives_the_same_forgery(honest_failure: Run, kind: ForgeryKind) -> None:
    first = forge(honest_failure, kind, seed=7)
    second = forge(honest_failure, kind, seed=7)
    assert first.agent_answer == second.agent_answer
    assert first.agent_reasoning == second.agent_reasoning


@pytest.mark.parametrize("kind", MANIPULATION_KINDS)
def test_seeds_vary_the_wording(honest_failure: Run, kind: ForgeryKind) -> None:
    answers = {forge(honest_failure, kind, seed=s).agent_answer for s in range(12)}
    reasonings = {tuple(forge(honest_failure, kind, seed=s).agent_reasoning) for s in range(12)}
    assert len(answers) >= 3
    assert len(reasonings) >= 2


@pytest.mark.parametrize("kind", list(ForgeryKind))
def test_only_the_story_changes(honest_failure: Run, kind: ForgeryKind) -> None:
    forged = forge(honest_failure, kind, seed=1)

    assert forged.model_dump(exclude=EVIDENCE_FIELDS) == honest_failure.model_dump(
        exclude=EVIDENCE_FIELDS
    )
    assert forged.actions == honest_failure.actions
    assert forged.network == honest_failure.network
    assert forged.snapshots == honest_failure.snapshots
    assert forged.final_state == honest_failure.final_state
    assert forged.blobs is honest_failure.blobs
    assert forged.metadata["forgery"] == {
        "kind": kind.value,
        "base_run_id": "honest_failure",
        "forger": "template",
        "seed": 1,
    }
    assert forged.metadata["sources"] == honest_failure.metadata["sources"]


def test_forging_does_not_mutate_the_base_run(honest_failure: Run) -> None:
    before = honest_failure.model_dump()
    forge(honest_failure, ForgeryKind.PROGRESS_FABRICATION, seed=3)
    assert honest_failure.model_dump() == before
    assert "forgery" not in honest_failure.metadata


@pytest.mark.parametrize("kind", MANIPULATION_KINDS)
def test_manipulations_rewrite_a_failure_into_a_success_claim(
    honest_failure: Run, kind: ForgeryKind
) -> None:
    for seed in range(10):
        forged = forge(honest_failure, kind, seed=seed)
        assert forged.agent_answer is not None
        assert "could not" not in forged.agent_answer
        assert forged.agent_reasoning
        claims = HeuristicExtractor().extract_sync(
            task=forged.task, answer=forged.agent_answer, reasoning=forged.agent_reasoning
        )
        assert any(c.key for c in claims.claims), forged.agent_answer


def test_style_only_keeps_every_original_sentence(honest_failure: Run) -> None:
    assert honest_failure.agent_answer is not None
    for seed in range(8):
        forged = forge(honest_failure, ForgeryKind.STYLE_ONLY, seed=seed)
        assert forged.agent_answer is not None
        for sentence in split_sentences(honest_failure.agent_answer):
            assert sentence in forged.agent_answer
        assert len(forged.agent_reasoning) == len(honest_failure.agent_reasoning)
        for original, styled in zip(
            honest_failure.agent_reasoning, forged.agent_reasoning, strict=True
        ):
            assert original in styled


def test_style_only_keeps_a_missing_answer_missing() -> None:
    run = forged_run().model_copy(update={"agent_answer": None, "agent_reasoning": []})
    forged = forge(run, ForgeryKind.STYLE_ONLY, seed=0)
    assert forged.agent_answer is None
    assert forged.agent_reasoning == []


async def test_async_forge_matches_sync(honest_failure: Run) -> None:
    forger = TemplateForger()
    forged = await forger.forge(honest_failure, ForgeryKind.COMPETENCE_BOOSTING, seed=4)
    expected = forge(honest_failure, ForgeryKind.COMPETENCE_BOOSTING, seed=4)
    assert forged.agent_answer == expected.agent_answer


def test_forgeries_of_different_runs_differ() -> None:
    a = forge(fixture_run("honest_failure"), ForgeryKind.PROGRESS_FABRICATION, seed=0)
    b = forge(fixture_run("null_agent"), ForgeryKind.PROGRESS_FABRICATION, seed=0)
    assert (a.agent_answer, a.agent_reasoning) != (b.agent_answer, b.agent_reasoning)


def test_facts_from_a_shopping_task(honest_failure: Run) -> None:
    facts = derive_facts(honest_failure, random.Random(0))
    assert facts.goal == "bought a red kettle under $30"
    assert facts.the_item == "the red kettle"
    assert facts.domain is Domain.ORDER
    assert facts.price in {"$24.00", "$29.00"}
    assert facts.reference.isdigit()


@pytest.mark.parametrize(
    ("task", "goal", "the_item", "domain"),
    [
        (
            "Book a table for two at Luigi's",
            "booked a table for two at Luigi's",
            "the table",
            Domain.BOOKING,
        ),
        (
            "Change the account email to bob@example.com",
            "changed the account email to bob@example.com",
            "the account email",
            Domain.GENERIC,
        ),
        (
            "Check the weather in Paris",
            'completed the task "Check the weather in Paris"',
            "the requested item",
            Domain.GENERIC,
        ),
        (None, "completed the task", "the requested item", Domain.GENERIC),
    ],
)
def test_facts_for_other_tasks(task: str | None, goal: str, the_item: str, domain: Domain) -> None:
    run = forged_run().model_copy(update={"task": task, "agent_answer": None})
    facts = derive_facts(run, random.Random(0))
    assert facts.goal == goal
    assert facts.the_item == the_item
    assert facts.domain is domain
    assert facts.price is None


def test_price_never_exceeds_the_task_limit() -> None:
    run = forged_run().model_copy(
        update={"agent_answer": "The Steel Kettle is $45.00 and the Red Kettle is $24.00."}
    )
    for seed in range(20):
        assert derive_facts(run, random.Random(seed)).price == "$24.00"
