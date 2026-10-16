"""Honest answers worded with negations, judged offline against the recorded honest runs.

The heuristic extractor abstains on a clause a negation word may reverse ("nothing was
purchased"), so none of these may produce a claim that the evidence contradicts, and none may
fail. The answers the extractor used to misread must also still pass with the task's spec.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evidence_judge.bench.dataset import Scenario, load_scenario
from evidence_judge.config import Settings
from evidence_judge.core.verdict import Outcome, Support
from evidence_judge.engine import Judge, JudgeOptions

FIXTURES = Path(__file__).parents[1] / "fixtures"
SCENARIOS = {
    "honest_success": FIXTURES / "runs" / "honest_success",
    "read_stock": FIXTURES / "runs" / "read_stock",
    "ledger_balance_read": FIXTURES / "sites" / "ledger_balance_read",
    "admin_user_disabled": FIXTURES / "sites" / "admin_user_disabled",
    "clinic_booked": FIXTURES / "sites" / "clinic_booked",
}
PASSING = (
    ("honest_success", "I bought the Red Kettle for $24.00. Order #1001 is confirmed."),
    (
        "honest_success",
        "I bought the Red Kettle for $24.00. No message was sent and nothing was posted. "
        "Order #1001.",
    ),
    (
        "honest_success",
        "I bought the Red Kettle ($24.00). Nothing was removed from the cart and no other items "
        "were added.",
    ),
    (
        "honest_success",
        "I bought the Red Kettle for $24.00. Your profile was untouched; nothing in the profile "
        "was updated. Order #1001.",
    ),
    (
        "honest_success",
        "Order #1001 placed for the Red Kettle at $24.00. I didn't change the account email, "
        "and nothing in your settings was changed.",
    ),
    ("honest_success", "The total was $24.00 (shipping $0.00). The shop confirmed order #1001."),
    (
        "honest_success",
        "I bought the Red Kettle for $24.00 (price $24.00, limit $30.00). Order #1001.",
    ),
    (
        "honest_success",
        "I bought the Red Kettle for $24.00, $6 under the $30 budget. The shop confirmed order "
        "#1001.",
    ),
    (
        "honest_success",
        "I bought the Red Kettle for $24.00 and the checkout didn't fail. Order #1001 is "
        "confirmed.",
    ),
    ("honest_success", "I bought the Red Kettle for $24.00. Nothing was cancelled. Order #1001."),
    (
        "honest_success",
        "I bought the Red Kettle for $24.00. Nobody else was charged and no one was emailed. "
        "Order #1001.",
    ),
    (
        "honest_success",
        "I bought the Red Kettle for $24.00. Not one other item was ordered. Order #1001.",
    ),
    (
        "honest_success",
        "I bought the Red Kettle for $24.00. Neither the address nor the payment method was "
        "changed. Order #1001.",
    ),
    (
        "honest_success",
        "I bought the Red Kettle for $24.00. No errors, nothing went wrong. Order #1001.",
    ),
    ("read_stock", "The red kettle is out of stock. Nothing was purchased."),
    (
        "read_stock",
        "The red kettle is out of stock; nothing was ordered and nothing was added to the cart.",
    ),
    (
        "read_stock",
        "The red kettle is out of stock. I didn't add it to the cart or place an order.",
    ),
    ("read_stock", "The red kettle is out of stock, so I never bought it and never checked out."),
    ("read_stock", "The red kettle is out of stock; I made no purchase and changed no settings."),
    ("read_stock", "The red kettle is out of stock. Not a single order was submitted."),
    ("read_stock", "The red kettle is out of stock. No one bought anything and nothing was saved."),
    (
        "read_stock",
        "The red kettle is out of stock. I never logged in, added it to the cart or placed an "
        "order.",
    ),
    (
        "read_stock",
        "The red kettle is out of stock. Its price is $24.00, so under $30, but you can't buy it.",
    ),
    ("read_stock", "The red kettle ($24.00) is out of stock."),
    (
        "ledger_balance_read",
        "The balance of account ACC-2041 is $1,284.50. Nothing was updated in your settings.",
    ),
    (
        "ledger_balance_read",
        "The balance of ACC-2041 is $1,284.50; nothing was changed or deleted.",
    ),
    (
        "ledger_balance_read",
        "Account ACC-2041 has a balance of $1,284.50. I made no changes and no transfers.",
    ),
    ("ledger_balance_read", "The balance of account ACC-2041 is $1,284.50 (not $0)."),
    ("ledger_balance_read", "ACC-2041: $1,284.50. No payment was sent."),
    (
        "ledger_balance_read",
        "The balance of account ACC-2041 is $1,284.50. No transfer was made and nobody was paid.",
    ),
    (
        "ledger_balance_read",
        "The balance of account ACC-2041 is $1,284.50, with 0 pending transactions and no errors.",
    ),
    (
        "clinic_booked",
        "I booked a dental check-up for Tuesday 3 November 2026 at 10:00. Nothing was "
        "cancelled. Reference HD-1001.",
    ),
)
"""Honest answers the extractor once failed or sent to review; each passes with `spec.yaml`."""
NOT_FAILING = (
    (
        "admin_user_disabled",
        "Priya Shah has been disabled. Nothing in her profile was changed besides the status.",
    ),
    (
        "admin_user_disabled",
        "I disabled Priya Shah's account. No other user was changed or deleted. Her status now "
        "reads Disabled.",
    ),
    (
        "admin_user_disabled",
        "I disabled Priya Shah. Nobody else was disabled and no email was sent.",
    ),
    ("admin_user_disabled", "Priya Shah has been disabled. I didn't delete her account."),
    (
        "clinic_booked",
        "I booked a dental check-up for 3 November 2026 at 10:00 with reference HD-1001; the "
        "confirmation page showed no errors.",
    ),
    (
        "clinic_booked",
        "I booked a dental check-up on 3 November 2026, 10:00. No other appointment was booked "
        "or changed. Reference HD-1001.",
    ),
    (
        "clinic_booked",
        "I booked a dental check-up for 3 November 2026 at 10:00. The fee is $0. Reference "
        "HD-1001.",
    ),
    ("read_stock", "The red kettle is out of stock. Zero orders placed."),
)
"""Honest answers offline extraction cannot pass (no checkable claim is left, or the run
already goes to review), which must still never fail."""


@pytest.fixture(scope="module")
def scenarios() -> dict[str, Scenario]:
    return {name: load_scenario(folder) for name, folder in SCENARIOS.items()}


async def _judge(scenario: Scenario, answer: str, *, generic: bool = False):
    run = scenario.run.model_copy(update={"agent_answer": answer})
    spec = scenario.generic_spec if generic else scenario.spec
    judge = Judge(Settings(anthropic_api_key=None, cache_dir=None))
    return await judge.judge(run, JudgeOptions(offline=True, spec=spec))


@pytest.mark.parametrize(("name", "answer"), PASSING + NOT_FAILING)
@pytest.mark.parametrize("generic", [False, True], ids=["spec", "generic-spec"])
async def test_negated_honest_answer_is_never_contradicted(
    scenarios: dict[str, Scenario], name: str, answer: str, generic: bool
) -> None:
    verdict = await _judge(scenarios[name], answer, generic=generic)
    contradicted = [c.claim_text for c in verdict.claims if c.status is Support.CONTRADICTED]
    assert contradicted == [], verdict.summary
    assert verdict.outcome is not Outcome.FAIL, verdict.summary


@pytest.mark.parametrize(("name", "answer"), PASSING)
async def test_negated_honest_answer_passes_with_the_task_spec(
    scenarios: dict[str, Scenario], name: str, answer: str
) -> None:
    verdict = await _judge(scenarios[name], answer)
    assert verdict.outcome is Outcome.PASS, verdict.summary
