"""Time budgets for judging many claims over long recordings.

Budgets are several times the measured cost on a laptop, so they catch per-predicate
re-normalisation of page text without flaking on a slow CI runner.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from evidence_judge.config import Settings
from evidence_judge.core.claims import Claim, ClaimKind, ClaimSet
from evidence_judge.core.evidence import Channel, PageSnapshot, PageState, Run
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import (
    PageTextPredicate,
    Predicate,
    Scope,
    UrlPredicate,
)
from evidence_judge.engine import Judge, JudgeOptions

BUDGET_S = 2.0
CLAIMS = 30
SNAPSHOTS = 2_000
SNAPSHOT_CHARS = 12_000


def _long_run() -> Run:
    filler = ("Lorem ipsum dolor sit amet, “consectetur” adipiscing elit — " * 250)[:SNAPSHOT_CHARS]
    snapshots = [
        PageSnapshot(
            id=f"snap#{i}", ts=float(i), url=f"https://shop.test/p/{i}", text=f"Page {i}. {filler}"
        )
        for i in range(SNAPSHOTS)
    ]
    return Run(
        run_id="long",
        source="synthetic",
        snapshots=snapshots,
        final_state=PageState(url="https://shop.test/done", text=filler),
        captured={Channel.SNAPSHOTS, Channel.FINAL_STATE, Channel.ACTIONS},
    )


def _claims() -> ClaimSet:
    claims: list[Claim] = []
    for i in range(CLAIMS):
        predicates: list[Predicate] = [
            PageTextPredicate(contains=f"missing phrase {i}", scope=Scope.ANY),
            PageTextPredicate(contains=f"final phrase {i}"),
            UrlPredicate(contains=f"/never/{i}", scope=Scope.ANY),
        ]
        claims.append(
            Claim(
                id=f"c{i}",
                text=f"claim {i}",
                quote=f"claim {i}",
                kind=ClaimKind.STATE,
                key=True,
                predicates=predicates,
            )
        )
    return ClaimSet(claims=claims)


@pytest.mark.perf
def test_thirty_claims_over_two_thousand_long_snapshots_judge_within_budget() -> None:
    run = _long_run()
    judge = Judge(Settings(_env_file=None, cache_dir=None))  # pyright: ignore[reportCallIssue]
    start = time.perf_counter()
    verdict = asyncio.run(judge.judge(run, JudgeOptions(offline=True, claims=_claims())))
    elapsed = time.perf_counter() - start
    assert len(verdict.claims) == CLAIMS
    assert elapsed < BUDGET_S, f"judging took {elapsed:.2f}s"


def test_normalized_text_is_computed_once_per_page_state() -> None:
    index = EvidenceIndex(_long_run())
    states = index.page_states()
    assert states is index.page_states()
    first = index.normalized_text(states[0])
    assert first is index.normalized_text(states[0])
    assert index.normalized_text(states[-1]) is index.normalized_text(index.final)  # type: ignore[arg-type]
