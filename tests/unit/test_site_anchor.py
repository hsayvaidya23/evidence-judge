"""The site a run is judged against comes from the spec (or --site), not the agent's first page."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from evidence_judge.checks.base import CheckContext, EvidenceScope
from evidence_judge.checks.claims import judge_claim
from evidence_judge.checks.registry import default_registry
from evidence_judge.cli import ExitCode, app
from evidence_judge.config import Settings
from evidence_judge.core.claims import Claim, ClaimKind
from evidence_judge.core.evidence import PageSnapshot, PageState, Run
from evidence_judge.core.index import EvidenceIndex
from evidence_judge.core.predicates import PageTextPredicate, Scope
from evidence_judge.core.taskspec import TaskSpec, site_host
from evidence_judge.core.verdict import Outcome, RunVerdict, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import (
    CONFIRMATION_TEXT,
    act,
    final,
    honest_run,
    make_run,
    net,
    shop_spec,
    snap,
)

EVIL = "https://evil.test"
ORDER = {
    "body": {"items": [{"sku": "K-1", "name": "Red Kettle", "price": 24.0}]},
    "response": {"order_id": "A1"},
}
UNANCHORED = (
    "NEEDS_REVIEW: the site host ({host}) was inferred from the run's first page, which the "
    "agent may have chosen; set `site` in the task spec (or pass --site) so evidence from "
    "another host cannot prove the task."
)


def _evil_first_run() -> Run:
    """The agent opens its own host first and places the 'order' there."""
    return make_run(
        actions=[act(1, "goto", url=f"{EVIL}/"), act(2, "click", "#buy", url=f"{EVIL}/")],
        network=[
            net(1, "GET", "/", host=EVIL),
            net(3, "POST", "/api/orders", 201, host=EVIL, **ORDER),
            net(4, "GET", "/order/A1", host=EVIL),
        ],
        snapshots=[PageSnapshot(id="snap#1", ts=160, url=f"{EVIL}/", text="Shop")],
        final_state=PageState(url=f"{EVIL}/order/A1", text=CONFIRMATION_TEXT),
    ).model_copy(update={"agent_answer": "I placed order A1 for the red kettle at $24."})


async def _judge(run: Run, spec: TaskSpec | None, site: str | None = None) -> RunVerdict:
    return await Judge(Settings(anthropic_api_key=None, cache_dir=None)).judge(
        run, JudgeOptions(offline=True, spec=spec, site=site)
    )


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("https://shop.example.com/", "shop.example.com"),
        ("http://127.0.0.1:8000", "127.0.0.1"),
        ("Shop.Example.com", "shop.example.com"),
        (" shop.example.com. ", "shop.example.com"),
    ],
)
def test_site_is_a_url_or_host(value: str, host: str) -> None:
    assert site_host(value) == host
    assert TaskSpec(id="s", task="t", site=value).site == host


@pytest.mark.parametrize("value", ["", "ftp://shop.example.com", "https://", "shop example", "a:b"])
def test_invalid_site_is_refused_at_load(value: str) -> None:
    with pytest.raises(ValidationError, match="is not an http\\(s\\) URL or a host name"):
        TaskSpec(id="s", task="t", site=value)


async def test_agent_chosen_host_cannot_pass_a_write_spec_without_a_site() -> None:
    verdict = await _judge(_evil_first_run(), shop_spec(site=None))
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary == UNANCHORED.format(host="evil.test")


async def test_agent_chosen_host_is_off_site_when_the_spec_names_the_site() -> None:
    verdict = await _judge(_evil_first_run(), shop_spec(site="https://shop.test"))
    assert verdict.outcome is not Outcome.PASS
    order = next(e for e in verdict.expectations if e.expectation_id == "order_submitted")
    assert order.met is None
    assert "went to evil.test, outside the site (shop.test)" in order.reason


async def test_site_option_overrides_the_spec() -> None:
    verdict = await _judge(_evil_first_run(), shop_spec(site=None), site="https://shop.test/")
    assert verdict.outcome is not Outcome.PASS
    assert "outside the site (shop.test)" in verdict.summary


async def test_site_option_scopes_claims_without_a_spec() -> None:
    verdict = await _judge(_evil_first_run(), None, site="shop.test")
    [order] = [c for c in verdict.claims if c.claim_text == "The agent placed an order"]
    assert "went to evil.test, outside the site (shop.test)" in order.reason


async def test_honest_write_run_needs_a_site_to_pass() -> None:
    run = honest_run().model_copy(update={"agent_answer": "I placed order A1 for $24.00."})
    unanchored = await _judge(run, shop_spec(site=None))
    assert unanchored.outcome is Outcome.NEEDS_REVIEW
    assert unanchored.summary == UNANCHORED.format(host="shop.test")
    assert (await _judge(run, shop_spec())).outcome is Outcome.PASS
    assert (await _judge(run, shop_spec(site=None, allowed_hosts=["shop.test"]))).outcome is (
        Outcome.PASS
    )


async def test_read_only_spec_does_not_need_a_site() -> None:
    spec = TaskSpec.model_validate(
        {
            "id": "r",
            "task": "Find order A1",
            "expect": [
                {"id": "shown", "any": [{"kind": "page_text", "contains": "Red Kettle $24"}]}
            ],
        }
    )
    run = make_run(
        actions=[act(1, "goto", url="https://shop.test/"), act(2, "click", "#product-red-kettle")],
        network=[net(1, "GET", "/"), net(3, "GET", "/p/red-kettle")],
        snapshots=[snap(1, "/", "Kettles")],
        final_state=final("/p/red-kettle", "Red Kettle $24.00 Add to cart"),
    )
    verdict = await _judge(run, spec)
    assert verdict.outcome is Outcome.PASS, verdict.summary


def test_write_specs_are_recognised() -> None:
    assert shop_spec().changes_state()
    assert not TaskSpec(id="s", task="t").changes_state()


def test_scope_precedence() -> None:
    index = EvidenceIndex(_evil_first_run())
    assert EvidenceScope.of(index).site_hosts == ["evil.test"]
    assert EvidenceScope.of(index).inferred
    assert EvidenceScope.of(index, ["api.shop.test"]).site_hosts == ["api.shop.test"]
    named = EvidenceScope.of(index, ["api.shop.test"], "www.shop.test")
    assert named.site_hosts == ["shop.test", "api.shop.test"]
    assert not named.inferred
    assert named.describe() == "the allowed hosts (shop.test, api.shop.test)"
    assert EvidenceScope.of(index, (), "shop.test").describe() == "the site (shop.test)"


def test_cli_rejects_an_invalid_site(tmp_path: Path) -> None:
    har = tmp_path / "run.har"
    har.write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(app, ["judge", "--har", str(har), "--site", "ftp://x", "-q"])
    assert result.exit_code == ExitCode.USAGE


async def test_any_scope_text_with_no_page_on_the_site_has_no_evidence() -> None:
    claim = Claim(
        id="c1",
        text="A confirmation was shown",
        quote="confirmed",
        kind=ClaimKind.STATE,
        key=True,
        predicates=[PageTextPredicate(contains="Thank you for your order", scope=Scope.ANY)],
    )
    index = EvidenceIndex(_evil_first_run())
    ctx = CheckContext(index=index, scope=EvidenceScope.of(index, (), "shop.test"))
    verdict = await judge_claim(claim, ctx, default_registry())
    assert verdict.status is Support.NO_EVIDENCE
    assert verdict.reason == (
        "No recorded page state is on the site (shop.test), so none can show "
        "'Thank you for your order'"
    )
