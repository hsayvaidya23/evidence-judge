"""Golden regression for the runs recorded on the clinic and admin-console demo sites, offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_judge.config import Settings
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.ingest import load_run

SITES = Path(__file__).parents[1] / "fixtures" / "sites"
SCENARIOS = sorted(p.name for p in SITES.iterdir() if (p / "expected.json").exists())
KNOWN_REVIEW = {"admin_user_disabled"}
"""Honest runs offline extraction cannot pass: 'I disabled Priya Shah's account' becomes no
checkable claim, so claim coverage is 0% and the run goes to review with every expectation met."""


def test_both_sites_and_tasks_are_recorded() -> None:
    assert SCENARIOS == [
        "admin_user_disabled",
        "admin_wrong_user",
        "clinic_booked",
        "clinic_weekend_rejected",
        "clinic_wrong_service",
        "ledger_balance_read",
        "ledger_unanswered",
        "ledger_wrong_account",
    ]


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_site_run_gets_expected_outcome(scenario: str) -> None:
    folder = SITES / scenario
    run = load_run(
        trace=folder / "trace.zip",
        answer_file=folder / "answer.txt",
        reasoning_file=folder / "reasoning.txt",
    )
    spec = TaskSpec.load(folder / "spec.yaml")
    expected = Outcome(json.loads((folder / "expected.json").read_text())["outcome"])

    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, spec=spec)
    )

    if scenario in KNOWN_REVIEW:
        assert verdict.outcome is Outcome.NEEDS_REVIEW
        assert "0% of the agent's answer became checkable claims" in verdict.summary
        assert all(e.met is True for e in verdict.expectations)
    else:
        assert verdict.outcome is expected, verdict.summary
    for claim in verdict.claims:
        if claim.status is Support.SUPPORTED:
            assert claim.citations, f"{claim.claim_id} is supported without citing evidence"


async def test_booking_and_confirmation_claims_are_checked_on_the_clinic() -> None:
    folder = SITES / "clinic_booked"
    run = load_run(
        trace=folder / "trace.zip",
        answer_file=folder / "answer.txt",
        reasoning_file=folder / "reasoning.txt",
    )
    verdict = await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, spec=TaskSpec.load(folder / "spec.yaml"))
    )
    supported = {c.claim_id for c in verdict.claims if c.status is Support.SUPPORTED}
    assert len(supported) == len(verdict.claims) >= 3
