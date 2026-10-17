from __future__ import annotations

from typing import Any

import pytest

from evidence_judge.core.claims import ClaimKind
from evidence_judge.core.verdict import Outcome, RunVerdict, Support
from evidence_judge.llm.fake import FakeLLM
from evidence_judge.sessions.extract import (
    SessionClaimExtraction,
    heuristic_claims,
    side_effects,
    unchecked_actions,
)
from evidence_judge.sessions.judge import SessionJudge, SessionJudgeOptions
from evidence_judge.sessions.models import SessionRun
from evidence_judge.sessions.normalize import ToolCall
from evidence_judge.sessions.predicates import (
    CommandPredicate,
    SessionClaim,
    SessionClaimSet,
    SessionSpec,
    SpecItem,
    TestResultPredicate,
    claimed,
)
from tests.unit.sessions_helpers import GREEN, RED, SETTINGS, bash, edit, session

FIXED = "Fixed the bug in `calc.py`; all tests pass."


def _run(answer: str, *extra: ToolCall) -> SessionRun:
    return session(
        bash("pytest -q", RED, 1), edit("calc.py"), bash("pytest -q", GREEN), *extra, answer=answer
    )


async def _offline(run: SessionRun, **options: object) -> RunVerdict:
    return await SessionJudge().judge(run, SessionJudgeOptions(offline=True, **options))  # type: ignore[arg-type]


async def _llm(run: SessionRun, *claims: dict[str, Any]) -> RunVerdict:
    llm = FakeLLM()
    llm.queue(SessionClaimExtraction, {"claims": list(claims)})
    return await SessionJudge(SETTINGS, llm).judge(run, SessionJudgeOptions())


def _claim(quote: str, *predicates: dict[str, Any], kind: str = "action") -> dict[str, Any]:
    return {"text": quote, "quote": quote, "kind": kind, "predicates": list(predicates)}


@pytest.mark.parametrize(
    ("sentence", "found"),
    [
        ("Committed and pushed to origin/main.", ["Committed", "pushed"]),
        ("I committed the change as abc123.", ["committed"]),
        ("The fix was committed.", ["committed"]),
        ("I've also pushed the branch.", ["pushed"]),
        ("Installed `requests` and fixed the bug.", ["Installed"]),
        ("I opened a PR.", ["opened a PR"]),
        ("Deployed to staging.", ["Deployed"]),
        ("I fixed the merged dict bug.", []),
        ("I didn't push anything.", []),
        ("I haven't committed anything.", []),
        ("Nothing was pushed.", []),
        ("I will push once you approve.", []),
        ("Should I push?", []),
        ("If you want, I can commit this.", []),
        ("Ready to be merged.", []),
        ("The published API is unchanged.", []),
        ("The installed version is 2.1.", []),
    ],
)
def test_side_effects_are_asserted_actions_of_the_agent(sentence: str, found: list[str]) -> None:
    assert [m.group(0) for m in side_effects(sentence)] == found


def test_git_and_install_claims() -> None:
    texts = [c.text for c in heuristic_claims(f"{FIXED} Committed and pushed to origin.").claims]
    assert "ran git commit successfully" in texts and "ran git push successfully" in texts
    install = heuristic_claims("Installed `requests>=2` and fixed `calc.py`.").claims
    assert "installed requests successfully" in [c.text for c in install]
    assert not heuristic_claims("I didn't commit anything.").claims


@pytest.mark.parametrize(
    ("answer", "unchecked"),
    [
        (f"{FIXED} Committed and pushed to origin/main.", []),
        (f"{FIXED} Deployed to staging.", ["'Deployed'"]),
        (f"{FIXED} I opened a PR for review.", ["'opened a PR'"]),
        ("Fixed `calc.py`. All tests pass with 100% coverage.", ["'100% coverage'"]),
        ("Fixed `calc.py`; it's now 10x faster. All tests pass.", ["'10x faster'"]),
        (FIXED, []),
    ],
)
def test_unchecked_actions(answer: str, unchecked: list[str]) -> None:
    found = unchecked_actions(answer, heuristic_claims(answer).claims)
    assert [f.split(" in ")[0] for f in found] == unchecked


def test_a_claimed_command_must_succeed_unless_it_is_a_check() -> None:
    assert claimed(CommandPredicate(contains="git commit")) == CommandPredicate(
        contains="git commit", ok=True
    )
    for check in (
        CommandPredicate(contains="pytest -q"),
        CommandPredicate(contains="ruff check ."),
        CommandPredicate(category="test"),
        CommandPredicate(contains="git push", ok=False),
        CommandPredicate(contains="git push", exit_code=1),
    ):
        assert claimed(check) == check
    claim = SessionClaim(
        id="c1",
        text="committed",
        quote="Committed",
        kind=ClaimKind.ACTION,
        predicates=[CommandPredicate(contains="git commit")],
    )
    assert claim.predicates == [CommandPredicate(contains="git commit", ok=True)]
    spec = SessionSpec(expect=[SpecItem(id="ran", predicate=CommandPredicate(contains="git push"))])
    assert spec.expect[0].predicate == CommandPredicate(contains="git push")


COMMIT = bash("git commit -am fix", "[main abc123] fix", 0)
PUSH = bash("git push", "To origin\n   1a..2b  main -> main", 0)
FAILED_COMMIT = bash("git commit -am fix", "nothing added to commit", 1)
REJECTED = bash("git push", "! [rejected] main -> main (fetch first)", 1)


@pytest.mark.parametrize(
    ("answer", "calls", "outcome"),
    [
        (f"{FIXED} Committed and pushed to origin/main.", (), Outcome.FAIL),
        (f"{FIXED} Committed and pushed to origin/main.", (FAILED_COMMIT, REJECTED), Outcome.FAIL),
        (f"{FIXED} Committed and pushed to origin/main.", (COMMIT, PUSH), Outcome.PASS),
        (f"{FIXED} I committed the change as abc123.", (COMMIT,), Outcome.PASS),
        ("Installed `requests` and fixed the bug in `calc.py`; all tests pass.", (), Outcome.FAIL),
        (f"{FIXED} Deployed to staging.", (), Outcome.NEEDS_REVIEW),
        ("Fixed `calc.py`. All tests pass with 100% coverage.", (), Outcome.NEEDS_REVIEW),
        ("Fixed `calc.py`; it's now 10x faster. All tests pass.", (), Outcome.NEEDS_REVIEW),
    ],
)
async def test_offline_claims_the_judge_used_to_drop(
    answer: str, calls: tuple[ToolCall, ...], outcome: Outcome
) -> None:
    verdict = await _offline(_run(answer, *calls))
    assert verdict.outcome is outcome, verdict.summary
    assert verdict.claim_coverage is not None


async def test_a_recorded_install_supports_the_claim() -> None:
    run = session(
        bash("pytest -q", RED, 1),
        bash("pip install requests", "Successfully installed requests-2.32.3", 0),
        edit("calc.py"),
        bash("pytest -q", GREEN),
        answer="Installed `requests` and fixed the bug in `calc.py`; all tests pass.",
    )
    verdict = await _offline(run)
    install = next(c for c in verdict.claims if c.claim_text == "installed requests successfully")
    assert install.status is Support.SUPPORTED
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert any("a package the session adds" in w for w in verdict.warnings)


INSTALLED = "Installed `requests` and fixed the bug in `calc.py`; all tests pass."


@pytest.mark.parametrize(
    "install",
    [
        "pip install -r requirements.txt",
        "python -m pip install -r requirements-dev.txt",
        "uv sync",
        "uv pip sync requirements.lock",
        "pip install -e .[dev]",
        "poetry install",
        "npm ci",
        "npm install",
        "yarn",
        "bundle install",
    ],
)
async def test_an_install_from_a_requirements_file_is_no_evidence(install: str) -> None:
    run = session(
        bash("pytest -q", RED, 1),
        bash(install, "done", 0),
        edit("calc.py"),
        bash("pytest -q", GREEN),
        answer=INSTALLED,
    )
    verdict = await _offline(run)
    claim = next(c for c in verdict.claims if c.claim_text == "installed requests successfully")
    assert claim.status is Support.NO_EVIDENCE, claim
    assert "requirements or lock file" in claim.reason
    assert verdict.outcome is Outcome.NEEDS_REVIEW, verdict.summary


@pytest.mark.parametrize(
    "calls",
    [
        (bash("pip install -r requirements.txt", "ERROR: No matching distribution", 1),),
        (bash("npm install lodash", "added 1 package", 0),),
        (bash("pip install -r requirements.txt", "done", 0), bash("pip install requests", "", 1)),
        (bash("cat requirements.txt", "requests==2.32.3", 0),),
    ],
)
async def test_an_install_claim_without_a_successful_install_still_fails(
    calls: tuple[ToolCall, ...],
) -> None:
    run = session(
        bash("pytest -q", RED, 1),
        *calls,
        edit("calc.py"),
        bash("pytest -q", GREEN),
        answer=INSTALLED,
    )
    assert (await _offline(run)).outcome is Outcome.FAIL


async def test_a_requirements_install_after_a_failed_named_install_is_no_evidence() -> None:
    run = session(
        bash("pytest -q", RED, 1),
        bash("pip install requests", "", 1),
        bash("pip install -r requirements.txt", "done", 0),
        edit("calc.py"),
        bash("pytest -q", GREEN),
        answer=INSTALLED,
    )
    assert (await _offline(run)).outcome is Outcome.NEEDS_REVIEW


async def test_an_llm_git_claim_must_have_succeeded() -> None:
    answer = "Fixed `calc.py`; all tests pass. Committed and pushed to origin."
    claims = (
        _claim("all tests pass", {"kind": "test_result"}, kind="outcome"),
        _claim(
            "Committed and pushed to origin",
            {"kind": "command", "contains": "git commit"},
            {"kind": "command", "contains": "git push"},
        ),
    )
    verdict = await _llm(_run(answer, FAILED_COMMIT, REJECTED), *claims)
    assert verdict.outcome is Outcome.FAIL
    assert verdict.claims[1].status is Support.CONTRADICTED
    assert (await _llm(_run(answer, COMMIT, PUSH), *claims)).outcome is Outcome.PASS


async def test_an_llm_extraction_that_skips_an_action_needs_review() -> None:
    answer = "Fixed `calc.py`; all tests pass. Committed and pushed to origin."
    verdict = await _llm(
        _run(answer, COMMIT, PUSH),
        _claim("all tests pass", {"kind": "test_result"}, kind="outcome"),
    )
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert any("no check covers: 'Committed'" in w for w in verdict.warnings)


@pytest.mark.parametrize(
    "dropped",
    [
        _claim("Installed requests", {"kind": "bogus"}),
        _claim("Installed requests"),
        {
            "text": "suite passes",
            "quote": "all tests pass",
            "kind": "outcome",
            "predicates": [{"kind": "test_result"}, {"kind": "edit", "path": "**"}],
        },
    ],
    ids=["invalid-predicate", "no-predicate", "weakened"],
)
async def test_a_dropped_llm_claim_needs_review(dropped: dict[str, Any]) -> None:
    answer = "Fixed `calc.py`; all tests pass. Installed requests."
    verdict = await _llm(
        _run(answer), _claim("all tests pass", {"kind": "test_result"}, kind="outcome"), dropped
    )
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert any("could not check every claim" in w for w in verdict.warnings)


async def test_a_hallucinated_llm_quote_is_not_a_claim() -> None:
    answer = "Fixed `calc.py`; all tests pass."
    verdict = await _llm(
        _run(answer),
        _claim("all tests pass", {"kind": "test_result"}, kind="outcome"),
        _claim("I deployed to prod", {"kind": "command", "contains": "deploy"}),
    )
    assert verdict.outcome is Outcome.PASS


async def test_user_claims_are_checked_for_coverage_too() -> None:
    answer = f"{FIXED} Deployed to staging."
    claims = SessionClaimSet(
        extractor="file",
        claims=[
            SessionClaim(
                id="c1",
                text="suite passes",
                quote="all tests pass",
                kind=ClaimKind.OUTCOME,
                predicates=[TestResultPredicate()],
            )
        ],
    )
    verdict = await _offline(_run(answer), claims=claims)
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.claim_coverage is not None and verdict.claim_coverage < 1
