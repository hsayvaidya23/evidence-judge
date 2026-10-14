from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from evidence_judge.bench import load_scenario
from evidence_judge.core.evidence import Run
from evidence_judge.core.predicates import Predicate, vacuity_reason
from evidence_judge.forgery import (
    INJECTED_PREDICATE_SETS,
    TAUTOLOGICAL_PREDICATE_SETS,
    ForgeryKind,
    forge,
    injected_claims,
    injected_proposals,
)

RUNS = Path(__file__).parents[1] / "fixtures" / "runs"
_PREDICATE: TypeAdapter[Predicate] = TypeAdapter(Predicate)


@pytest.fixture(scope="module")
def null_agent() -> Run:
    return load_scenario(RUNS / "null_agent").run


@pytest.mark.parametrize("raw", [p for s in INJECTED_PREDICATE_SETS for p in s])
def test_every_injected_predicate_is_vacuous_or_too_weak(raw: dict[str, object]) -> None:
    try:
        predicate = _PREDICATE.validate_python(raw)
    except ValidationError:
        return
    assert vacuity_reason(predicate) is not None


@pytest.mark.parametrize("seed", range(4))
def test_injected_claims_keep_only_tautological_predicates(null_agent: Run, seed: int) -> None:
    forged = forge(null_agent, ForgeryKind.INJECTED_PREDICATES, seed=seed)
    claims = injected_claims(forged, seed=seed)
    tautological = {p["kind"] for s in TAUTOLOGICAL_PREDICATE_SETS for p in s}
    assert len(claims.claims) == 2
    assert all(c.key for c in claims.claims)
    assert all(p.kind in tautological for c in claims.claims for p in c.predicates)
    assert all(c.quote in (forged.agent_answer or "") for c in claims.claims)
    assert claims.extractor == "injected"
    assert "hallucinated_quotes=0" in (claims.coverage_note or "")


def test_injection_is_deterministic(null_agent: Run) -> None:
    forged = forge(null_agent, ForgeryKind.INJECTED_PREDICATES, seed=2)
    assert injected_proposals(forged, seed=2) == injected_proposals(forged, seed=2)
    assert injected_claims(forged, seed=2) == injected_claims(forged, seed=2)
