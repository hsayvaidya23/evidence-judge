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
    honest_run,
    make_run,
    net,
    shop_spec,
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


def _read_spec(site: str | None) -> TaskSpec:
    return TaskSpec.model_validate(
        {
            "id": "r",
            "task": "Find the red kettle's price",
            "site": site,
            "expect": [
                {"id": "shown", "any": [{"kind": "page_text", "contains": "Red Kettle $24"}]}
            ],
        }
    )


def _read_run(host: str = "https://shop.test") -> Run:
    return make_run(
        actions=[act(1, "goto", url=f"{host}/"), act(2, "click", "#product-red-kettle")],
        network=[net(1, "GET", "/", host=host), net(3, "GET", "/p/red-kettle", host=host)],
        snapshots=[PageSnapshot(id="snap#1", ts=160, url=f"{host}/", text="Kettles")],
        final_state=PageState(url=f"{host}/p/red-kettle", text="Red Kettle $24.00 Add to cart"),
    )


async def test_read_only_spec_needs_a_site_to_pass() -> None:
    unanchored = await _judge(_read_run(), _read_spec(None))
    assert unanchored.outcome is Outcome.NEEDS_REVIEW
    assert unanchored.summary == UNANCHORED.format(host="shop.test")
    assert all(e.met is True for e in unanchored.expectations)
    assert (await _judge(_read_run(), _read_spec("shop.test"))).outcome is Outcome.PASS
    assert (await _judge(_read_run(), _read_spec(None), site="shop.test")).outcome is Outcome.PASS


async def test_read_answered_on_the_agents_own_host_does_not_pass() -> None:
    run = _read_run(EVIL)
    assert (await _judge(run, _read_spec(None))).outcome is Outcome.NEEDS_REVIEW
    named = await _judge(run, _read_spec("shop.test"))
    assert named.outcome is not Outcome.PASS
    assert named.expectations[0].met is not True


def _lookup_run(answer_host: str, start_host: str = "https://shop.test") -> Run:
    """Opens the start page, then reads order A77's status on `answer_host`."""
    return make_run(
        actions=[
            act(1, "goto", url=f"{start_host}/"),
            act(2, "click", "#orders", url=f"{start_host}/"),
        ],
        network=[
            net(1, "GET", "/", host=start_host),
            net(3, "GET", "/orders/A77", host=answer_host, response="Order A77 has shipped"),
        ],
        snapshots=[PageSnapshot(id="snap#1", ts=160, url=f"{start_host}/", text="Welcome")],
        final_state=PageState(url=f"{answer_host}/orders/A77", text="Order A77 has shipped"),
    ).model_copy(update={"agent_answer": "Order A77 has shipped."})


async def test_no_spec_run_on_one_site_can_pass() -> None:
    verdict = await _judge(_lookup_run("https://shop.test"), None)
    assert verdict.outcome is Outcome.PASS, verdict.summary


async def test_no_spec_run_on_several_sites_needs_a_site_to_pass() -> None:
    verdict = await _judge(_lookup_run("https://evil.test"), None)
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary == (
        "NEEDS_REVIEW: the run visited several sites (shop.test, evil.test); which one is the "
        "site is ambiguous; pass --site."
    )
    anchored = await _judge(_lookup_run("https://evil.test"), None, site="shop.test")
    assert anchored.outcome is not Outcome.PASS
    assert "several sites" not in anchored.summary


@pytest.mark.parametrize("answer_host", ["https://www.shop.test", "https://api.shop.test"])
async def test_subdomains_and_www_are_one_site(answer_host: str) -> None:
    verdict = await _judge(_lookup_run(answer_host), None)
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
