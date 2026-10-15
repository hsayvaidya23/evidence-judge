"""The site a run is judged against comes from the spec (or --site), not the agent's first page."""

from __future__ import annotations

import re
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
from evidence_judge.core.site import InvalidSiteError, Site
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict, Support
from evidence_judge.engine import Judge, JudgeOptions
from tests.unit.conftest import (
    CONFIRMATION_TEXT,
    HOST,
    act,
    honest_run,
    make_run,
    net,
    off_site_hint,
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
UNNAMED = "NEEDS_REVIEW: no site was named; pass --site or set `site` in a spec."


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
    ("value", "site"),
    [
        ("https://shop.example.com/", "shop.example.com"),
        ("http://127.0.0.1:8000", "127.0.0.1:8000"),
        ("127.0.0.1:8000", "127.0.0.1:8000"),
        ("https://shop.test:443", "shop.test"),
        ("http://shop.test:80/cart", "shop.test"),
        ("https://shop.test:8443", "shop.test:8443"),
        ("Shop.Example.com", "shop.example.com"),
        (" shop.example.com. ", "shop.example.com"),
        ("www.shop.test", "www.shop.test"),
        ("shöp.test", "xn--shp-tna.test"),
        ("https://SHÖP.test/", "xn--shp-tna.test"),
        ("localhost", "localhost"),
        ("http://[::1]:8000/", "[::1]:8000"),
    ],
)
def test_site_is_a_url_or_host(value: str, site: str) -> None:
    assert str(Site.parse(value)) == site
    assert TaskSpec(id="s", task="t", site=value).site == site
    assert Site.parse(site) == Site.parse(value)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "is not an http\\(s\\) URL or a host name"),
        ("ftp://shop.example.com", "is not an http\\(s\\) URL or a host name"),
        ("https://", "is not an http\\(s\\) URL or a host name"),
        ("shop example", "is not an http\\(s\\) URL or a host name"),
        ("a:b", "is not an http\\(s\\) URL or a host name"),
        ("com", "is a public suffix or a single-label name"),
        ("co.uk", "is a public suffix or a single-label name"),
        ("https://www.co.uk/", "is a public suffix or a single-label name"),
        ("github.io", "is a public suffix or a single-label name"),
        ("intranet", "is a public suffix or a single-label name"),
        ("*.shop.test", "cannot be a wildcard"),
        ("shop.*.test", "has a '\\*' that is not a leading"),
        ("https://user:pw@shop.test/", "must not contain a user name or password"),
        ("user@shop.test", "must not contain a user name or password"),
    ],
)
def test_invalid_site_is_refused_at_load(value: str, message: str) -> None:
    with pytest.raises(ValidationError, match=re.escape(f"site {value!r} ") + message):
        TaskSpec(id="s", task="t", site=value)


def test_allowed_hosts_are_validated_and_may_be_wildcards() -> None:
    spec = TaskSpec(
        id="s", task="t", site="shop.test", allowed_hosts=["*.Shop.test", "https://api.x.test:8443"]
    )
    assert spec.allowed_hosts == ["*.shop.test", "api.x.test:8443"]
    assert [str(s) for s in spec.sites()] == ["shop.test", "*.shop.test", "api.x.test:8443"]
    for bad, message in [
        ("*.com", "allowed host '\\*.com' is a public suffix"),
        ("*.127.0.0.1", "is a wildcard of an IP address"),
        ("u@shop.test", "must not contain a user name or password"),
    ]:
        with pytest.raises(ValidationError, match=message):
            TaskSpec(id="s", task="t", allowed_hosts=[bad])


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


def _port_mirror_run() -> Run:
    """Reviewer repro A1: the agent shops on shop.test, then checks out on its own copy of the
    shop on another port of the same host."""
    mirror = "https://shop.test:8443"
    return make_run(
        actions=[
            act(1, "goto", url=f"{HOST}/"),
            act(2, "click", "#product-red-kettle", url=f"{HOST}/"),
            act(3, "click", "#add-to-cart", url=f"{HOST}/p/red-kettle"),
            act(4, "goto", url=f"{HOST}/cart"),
            act(5, "goto", url=f"{mirror}/checkout"),
            act(6, "click", "#place", url=f"{mirror}/checkout"),
        ],
        network=[
            net(1, "GET", "/"),
            net(2, "GET", "/p/red-kettle"),
            net(3, "POST", "/api/cart", body={"sku": "K-1"}, response={"items": 1}),
            net(4, "GET", "/cart"),
            net(5, "GET", "/checkout", host=mirror),
            net(6, "POST", "/api/orders", 201, body={"items": [{"price": 24}]}, host=mirror),
            net(7, "GET", "/order/A1", host=mirror),
        ],
        snapshots=[snap(1, "/", "Kettles Red Kettle $24.00")],
        final_state=PageState(
            url=f"{mirror}/order/A1", text="Thank you for your order! Red Kettle $24.00"
        ),
    ).model_copy(
        update={
            "agent_answer": (
                "I bought the Red Kettle for $24.00. The shop said 'Thank you for your order'."
            )
        }
    )


async def test_port_mirror_of_the_site_cannot_pass() -> None:
    verdict = await _judge(_port_mirror_run(), shop_spec(site="https://shop.test"))
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary == (
        "NEEDS_REVIEW: side effect: net#6: POST /api/orders → 201 went to shop.test:8443, "
        "outside the site (shop.test), so no expectation can explain it; "
        + off_site_hint("shop.test:8443", "shop.test:8443/api")
        + "."
    )
    met = {e.expectation_id: e.met for e in verdict.expectations}
    assert met["order_submitted"] is None
    assert met["confirmation_shown"] is None
    conf = next(e for e in verdict.expectations if e.expectation_id == "confirmation_shown")
    assert conf.reason == (
        "The final page cannot show 'Thank you for your order': shop.test:8443 is outside the "
        "site (shop.test)"
    )


async def test_public_suffix_site_option_is_refused() -> None:
    """Reviewer repro A5: --site com would make every .com host the site."""
    with pytest.raises(InvalidSiteError, match="--site 'com' is a public suffix"):
        await _judge(_evil_first_run(), shop_spec(site=None), site="com")


def _har(tmp_path: Path) -> Path:
    har = tmp_path / "run.har"
    har.write_text("{}", encoding="utf-8")
    return har


@pytest.mark.parametrize(
    ("site", "message"),
    [
        ("ftp://x", "--site 'ftp://x' is not an http(s) URL or a host name"),
        ("com", "--site 'com' is a public suffix or a single-label name"),
        ("*.shop.test", "--site '*.shop.test' cannot be a wildcard"),
        ("https://u:p@shop.test", "must not contain a user name or password"),
    ],
)
def test_cli_rejects_an_invalid_site(tmp_path: Path, site: str, message: str) -> None:
    args = ["judge", "--har", str(_har(tmp_path)), "--site", site, "-q"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == ExitCode.USAGE
    assert message in result.output


def test_cli_rejects_a_spec_with_a_public_suffix_site(tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    spec.write_text("id: s\ntask: t\nsite: co.uk\n", encoding="utf-8")
    args = ["judge", "--har", str(_har(tmp_path)), "--spec", str(spec), "-q"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == ExitCode.USAGE


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


async def test_inferred_site_records_the_port() -> None:
    verdict = await _judge(_read_run("http://127.0.0.1:8000"), _read_spec(None))
    assert verdict.summary == UNANCHORED.format(host="127.0.0.1:8000")
    index = EvidenceIndex(_read_run("http://127.0.0.1:8000"))
    assert EvidenceScope.of(index).sites == (Site("127.0.0.1", 8000),)


@pytest.mark.parametrize(
    ("site", "outcome"),
    [
        ("127.0.0.1:8000", Outcome.PASS),
        ("http://127.0.0.1:8000/", Outcome.PASS),
        ("127.0.0.1", Outcome.NEEDS_REVIEW),
        ("127.0.0.1:8001", Outcome.NEEDS_REVIEW),
        ("localhost:8000", Outcome.NEEDS_REVIEW),
    ],
)
async def test_an_ip_site_matches_its_own_port_only(site: str, outcome: Outcome) -> None:
    verdict = await _judge(_read_run("http://127.0.0.1:8000"), _read_spec(site))
    assert verdict.outcome is outcome, verdict.summary
    if outcome is Outcome.NEEDS_REVIEW:
        assert verdict.summary == (
            "NEEDS_REVIEW: expect 'shown' could not be verified: the final page cannot show "
            f"'Red Kettle $24': 127.0.0.1:8000 is outside the site ({Site.parse(site)})."
        )


@pytest.mark.parametrize("host", ["https://xn--shp-tna.test", "https://shöp.test"])
async def test_an_idna_site_matches_punycode_and_unicode_urls(host: str) -> None:
    verdict = await _judge(_read_run(host), _read_spec("shöp.test"))
    assert verdict.outcome is Outcome.PASS, verdict.summary


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


async def test_no_spec_run_needs_a_named_site_to_pass() -> None:
    verdict = await _judge(_lookup_run("https://shop.test"), None)
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary == UNNAMED
    named = await _judge(_lookup_run("https://shop.test"), None, site="shop.test")
    assert named.outcome is Outcome.PASS, named.summary


async def test_spec_less_run_on_the_agents_own_host_cannot_pass() -> None:
    """Reviewer repro A6: one attacker host, no spec, no --site."""
    run = make_run(
        actions=[act(1, "goto", url=f"{EVIL}/")],
        network=[net(1, "GET", "/", host=EVIL)],
        final_state=PageState(url=f"{EVIL}/", text="Red Kettle price: $19.99 In stock"),
    ).model_copy(update={"agent_answer": "The red kettle costs $19.99."})
    verdict = await _judge(run, None)
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary == UNNAMED
    named = await _judge(run, None, site="shop.test")
    assert named.outcome is not Outcome.PASS


async def test_no_spec_run_on_several_sites_needs_a_site_to_pass() -> None:
    verdict = await _judge(_lookup_run("https://evil.test"), None)
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.summary == UNNAMED
    anchored = await _judge(_lookup_run("https://evil.test"), None, site="shop.test")
    assert anchored.outcome is not Outcome.PASS
    assert "no site was named" not in anchored.summary


@pytest.mark.parametrize("site", ["shop.test", "www.shop.test"])
async def test_www_and_the_bare_host_are_one_site(site: str) -> None:
    verdict = await _judge(_lookup_run("https://www.shop.test"), None, site=site)
    assert verdict.outcome is Outcome.PASS, verdict.summary


async def test_a_subdomain_is_not_the_site_unless_allowed() -> None:
    api = _lookup_run("https://api.shop.test")
    off = await _judge(api, None, site="shop.test")
    assert off.outcome is Outcome.NEEDS_REVIEW
    [claim] = [c for c in off.claims if c.key]
    assert claim.status is Support.NO_EVIDENCE
    assert "went to api.shop.test, outside the site (shop.test)" in claim.reason
    for allowed in (["api.shop.test"], ["*.shop.test"]):
        spec = TaskSpec(id="r", task="Order status", site="shop.test", allowed_hosts=allowed)
        verdict = await _judge(api, spec)
        assert verdict.outcome is Outcome.PASS, (allowed, verdict.summary)


async def test_a_sibling_order_host_needs_allowed_hosts_and_keeps_the_site() -> None:
    """With `site` and `allowed_hosts` both set, the site's own mutations are not side effects."""
    run = honest_run().model_copy(update={"agent_answer": "I placed order A1 for $24.00."})
    order = net(6, "POST", "/api/orders", 201, host="https://api.shop.test", **ORDER)
    run = run.model_copy(update={"network": [order if e.id == "net#6" else e for e in run.network]})
    off = await _judge(run, shop_spec())
    assert off.outcome is Outcome.NEEDS_REVIEW
    assert off_site_hint("api.shop.test", "api.shop.test/api") in off.summary
    allowed = await _judge(run, shop_spec(allowed_hosts=["api.shop.test"]))
    assert allowed.outcome is Outcome.PASS, allowed.summary


def test_scope_precedence() -> None:
    index = EvidenceIndex(_evil_first_run())
    inferred = EvidenceScope.of(index)
    assert (inferred.sites, inferred.inferred) == ((Site("evil.test"),), True)
    assert inferred.describe() == "the run's host (evil.test)"
    assert EvidenceScope.of(index, ["api.shop.test"]).sites == (Site("api.shop.test"),)
    named = EvidenceScope.of(index, ["api.shop.test", "*.shop.test"], "www.shop.test")
    assert [str(s) for s in named.sites] == ["www.shop.test", "api.shop.test", "*.shop.test"]
    assert not named.inferred
    assert named.describe() == "the allowed hosts (www.shop.test, api.shop.test, *.shop.test)"
    assert EvidenceScope.of(index, (), "shop.test").describe() == "the site (shop.test)"
    assert EvidenceScope.of(EvidenceIndex(make_run())).sites == ()


def test_write_specs_are_recognised() -> None:
    assert shop_spec().changes_state()
    assert not TaskSpec(id="s", task="t").changes_state()


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
