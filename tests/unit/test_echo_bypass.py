"""Echoes of agent input that differ from what was typed still prove nothing.

Each run shows "Order A77 confirmed" on the final page only because the site repeats something
the agent supplied, in a form that is not an exact copy of the recorded action value.
"""

from __future__ import annotations

import pytest

from evidence_judge.checks.claims import judge_claim
from evidence_judge.checks.registry import default_registry
from evidence_judge.core.claims import Claim, ClaimKind
from evidence_judge.core.evidence import ActionEvent, Run
from evidence_judge.core.predicates import PageTextPredicate
from evidence_judge.core.verdict import Support
from tests.unit.conftest import HOST, act, ctx, final, make_run, net, snap

CLAIM = Claim(
    id="c1",
    text="Order A77 confirmed",
    quote="Order A77 confirmed",
    kind=ClaimKind.OUTCOME,
    key=True,
    predicates=[PageTextPredicate(contains="Order A77 confirmed")],
)
ECHO_REASON = "it holds only where the page or URL echoes the agent's own input"


def _run(action: ActionEvent, final_text: str) -> Run:
    return make_run(
        actions=[act(1, "goto", url=f"{HOST}/"), action],
        network=[net(1, "GET", "/"), net(5, "GET", "/profile")],
        snapshots=[snap(1, "/", "Welcome")],
        final_state=final("/profile", final_text),
    )


ECHOES = {
    "typed exactly": (
        act(2, "fill", "#q", value="Order A77 confirmed"),
        "You wrote Order A77 confirmed",
    ),
    "uploaded file name": (
        act(2, "upload", "#file", value="Order A77 confirmed.pdf"),
        "Uploaded: Order A77 confirmed.pdf",
    ),
    "uploaded file stem from a path": (
        act(2, "upload_file", "#file", value="/tmp/run/Order A77 confirmed.pdf"),
        "Your document Order A77 confirmed was received",
    ),
    "pressed text (browser-use send_keys)": (
        act(2, "send_keys", "#q", value="Order A77 confirmed"),
        "You wrote Order A77 confirmed",
    ),
    "typed markup rendered": (
        act(2, "fill", "#bio", value="<b>Order A77</b> confirmed"),
        "Bio: Order A77 confirmed",
    ),
    "typed block markup rendered": (
        act(2, "fill", "#bio", value="<p>Order A77</p><p>confirmed</p>"),
        "Bio: Order A77\nconfirmed",
    ),
    "typed character references decoded": (
        act(2, "fill", "#bio", value="Order&#32;A77 confirmed"),
        "Bio: Order A77 confirmed",
    ),
}

GENUINE = {
    "key chord press": (
        act(2, "press", "#q", value="Control+Enter"),
        "Order A77 confirmed",
    ),
    "unrelated upload": (
        act(2, "upload", "#file", value="receipt.pdf"),
        "Order A77 confirmed",
    ),
}


@pytest.mark.parametrize("case", ECHOES)
async def test_echo_of_agent_input_is_no_evidence(case: str) -> None:
    action, final_text = ECHOES[case]
    verdict = await judge_claim(CLAIM, ctx(_run(action, final_text)), default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert ECHO_REASON in verdict.reason


@pytest.mark.parametrize("case", GENUINE)
async def test_page_text_beyond_agent_input_is_evidence(case: str) -> None:
    action, final_text = GENUINE[case]
    verdict = await judge_claim(CLAIM, ctx(_run(action, final_text)), default_registry())
    assert verdict.status is Support.SUPPORTED
