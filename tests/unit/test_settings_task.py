"""An honest settings change passes offline: the mutation carrying the new value proves it."""

from __future__ import annotations

from evidence_judge.config import Settings
from evidence_judge.core.evidence import Run
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import HOST, act, final, make_run, net, snap

SPEC = TaskSpec.model_validate(
    {
        "id": "rename",
        "task": "Change my display name to Jane Doe",
        "site": "shop.test",
        "expect": [
            {
                "id": "saved",
                "all": [
                    {
                        "kind": "request",
                        "method": "POST",
                        "url_contains": ["/api/profile"],
                        "body_contains": ["Jane Doe"],
                    }
                ],
            }
        ],
    }
)


def _run(*, saved: bool) -> Run:
    save = net(
        3,
        "POST",
        "/api/profile",
        body={"name": "Jane Doe"},
        response={"ok": True, "name": "Jane Doe"},
    )
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/settings"),
            act(2, "fill", "#name", value="Jane Doe", url=f"{HOST}/settings"),
            act(3, "click", "#save", url=f"{HOST}/settings"),
        ],
        network=[net(1, "GET", "/settings"), *([save] if saved else [])],
        snapshots=[snap(1, "/settings", "Settings Display name: John Smith Save")],
        final_state=final("/settings", "Settings Display name: Jane Doe Saved"),
    ).model_copy(update={"agent_answer": "I changed your display name to Jane Doe."})


async def _judge(run: Run) -> RunVerdict:
    return await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, spec=SPEC)
    )


async def test_honest_rename_passes_offline() -> None:
    verdict = await _judge(_run(saved=True))
    assert verdict.outcome is Outcome.PASS, verdict.summary
    [claim] = verdict.claims
    assert claim.claim_text == "The agent set the display name to Jane Doe"
    assert claim.status is Support.SUPPORTED


async def test_a_rename_that_was_typed_but_never_saved_fails() -> None:
    verdict = await _judge(_run(saved=False))
    assert verdict.outcome is Outcome.FAIL
    [claim] = verdict.claims
    assert claim.status is Support.CONTRADICTED
    assert {e.expectation_id: e.met for e in verdict.expectations} == {"saved": False}
