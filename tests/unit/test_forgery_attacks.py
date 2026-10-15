"""The forgeries for attacks that once produced false PASSes, and their honest controls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evidence_judge.bench import load_scenario
from evidence_judge.bench.dataset import Scenario
from evidence_judge.config import Settings
from evidence_judge.core.actions import canonical_action
from evidence_judge.core.evidence import Run
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict, Support
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.forgery import ForgeryKind, forge, plant_evidence
from evidence_judge.forgery.composed import (
    CLICK_VARIANTS,
    FRAGMENT_VARIANTS,
    MARKUP_VARIANTS,
    SHORT_VARIANTS,
)
from evidence_judge.forgery.controls import DETOUR_URL, ControlKind, apply_control
from evidence_judge.forgery.echoes import (
    BYPASS_VARIANTS,
    DERIVED_VARIANTS,
    ECHO_VARIANTS,
    LINK_VARIANTS,
    RESPONSE_VARIANTS,
    TRANSFORMED_VARIANTS,
)
from evidence_judge.forgery.evidence import UNANCHORED_KINDS, WRITE_ONLY_KINDS, unanchored
from evidence_judge.forgery.hosts import MIRROR_ORIGIN, OFFSITE_VARIANTS, port_mirror_origin
from evidence_judge.forgery.inputs import (
    COMPOSE_VARIANTS,
    KEYBOARD_VARIANTS,
    SCRIPT_URL_VARIANTS,
    SELECT_VARIANTS,
    UPLOAD_VARIANTS,
)
from evidence_judge.forgery.outcomes import REDIRECT_LANDINGS
from evidence_judge.forgery.planting import is_write_task
from evidence_judge.forgery.tampering import (
    CHANNEL_VARIANTS,
    CUSTOM_TOOL_VARIANTS,
    INJECTION_VARIANTS,
    MOCK_VARIANTS,
    SCREENSHOT_VARIANTS,
    STORAGE_VARIANTS,
)
from evidence_judge.forgery.unshown import (
    HIDDEN_VARIANTS,
    SOURCE_VARIANTS,
    UNMODELLED_VARIANTS,
    UNRENDERED_VARIANTS,
)

RUNS = Path(__file__).parents[1] / "fixtures" / "runs"
NEW_KINDS = (
    ForgeryKind.EXPECT_ECHO,
    ForgeryKind.ECHO_BYPASS,
    ForgeryKind.OFFSITE_CONFIRMATION,
    ForgeryKind.AGENT_CHOSEN_HOST,
    ForgeryKind.PORT_MIRROR,
    ForgeryKind.REDIRECT_TO_ERROR,
    ForgeryKind.BASELINE_TAUTOLOGY,
    ForgeryKind.RESPONSE_ECHO,
    ForgeryKind.SCRIPT_INJECTION,
    ForgeryKind.MOCKED_RESPONSE,
    ForgeryKind.DATA_URL_LINK,
    ForgeryKind.STORAGE_TAMPERING,
    ForgeryKind.SOCKET_OR_CDP,
    ForgeryKind.KEYBOARD_ECHO,
    ForgeryKind.JAVASCRIPT_URL,
    ForgeryKind.PATH_UPLOAD,
    ForgeryKind.OPTION_SELECT,
    ForgeryKind.DIRECTORY_UPLOAD,
    ForgeryKind.STYLED_SCREENSHOT,
    ForgeryKind.CUSTOM_TOOL,
    ForgeryKind.FIELD_COMPOSE,
    ForgeryKind.TRANSFORMED_ECHO,
    ForgeryKind.VIEW_SOURCE,
    ForgeryKind.HIDDEN_TEXT,
    ForgeryKind.SHORT_COMPOSE,
    ForgeryKind.MARKUP_ECHO,
    ForgeryKind.FRAGMENT_ECHO,
    ForgeryKind.CLICK_COMPOSE,
    ForgeryKind.UNMODELLED_HIDING,
    ForgeryKind.DERIVED_ECHO,
    ForgeryKind.UNRENDERED_MARKUP,
    ForgeryKind.MUTATING_GET,
)
F, R, P = Outcome.FAIL, Outcome.NEEDS_REVIEW, Outcome.PASS
ECHO = "echoes the agent's own input"
BEFORE = "already true before the agent acted"
UNANCHORED = "was inferred from the run's first page"
MADE = "it holds only on evidence the agent made itself"


@pytest.fixture(scope="module")
def scenarios() -> dict[str, Scenario]:
    names = (
        "null_agent",
        "read_unanswered",
        "honest_success",
        "loop",
        "read_stock",
        "forged_success",
    )
    return {name: load_scenario(RUNS / name) for name in names}


def _forged(scenario: Scenario, kind: ForgeryKind, seed: int) -> Run:
    specs = (scenario.spec, scenario.generic_spec)
    return plant_evidence(forge(scenario.run, kind, seed=seed), kind, seed=seed, specs=specs)


async def _verdicts(
    scenario: Scenario, run: Run, kind: ForgeryKind | None = None
) -> tuple[RunVerdict, RunVerdict, RunVerdict]:
    """With the scenario's spec, with its generic spec and with no spec, as the bench judges."""
    spec, generic = scenario.spec, scenario.generic_spec
    if kind in UNANCHORED_KINDS:
        spec, generic = unanchored(spec), unanchored(generic)
    judge = Judge(Settings(anthropic_api_key=None, cache_dir=None))
    options = [JudgeOptions(offline=True, spec=s) for s in (spec, generic, None)]
    first, second, third = [await judge.judge(run, o) for o in options]
    return first, second, third


def test_kind_groups() -> None:
    assert {
        ForgeryKind.THIRD_PARTY_ORDER,
        ForgeryKind.REDIRECT_TO_ERROR,
        ForgeryKind.BASELINE_TAUTOLOGY,
    } == WRITE_ONLY_KINDS
    assert {ForgeryKind.AGENT_CHOSEN_HOST} == UNANCHORED_KINDS


def test_read_task_is_recognised(scenarios: dict[str, Scenario]) -> None:
    read = scenarios["read_unanswered"]
    assert not is_write_task((read.spec, read.generic_spec))
    write = scenarios["null_agent"]
    assert is_write_task((write.spec, write.generic_spec))
    assert is_write_task(())


def test_unanchored_drops_site_and_allowed_hosts() -> None:
    spec = TaskSpec(id="s", task="t", site="shop.test", allowed_hosts=["api.shop.test"])
    bare = unanchored(spec)
    assert bare is not None
    assert (bare.site, bare.allowed_hosts) == (None, [])
    assert unanchored(None) is None


@pytest.mark.parametrize("kind", NEW_KINDS)
@pytest.mark.parametrize("seed", range(3))
def test_planting_is_deterministic_and_keeps_the_recording(
    scenarios: dict[str, Scenario], kind: ForgeryKind, seed: int
) -> None:
    base = scenarios["null_agent"]
    run = _forged(base, kind, seed)
    assert run == _forged(base, kind, seed)
    assert [a for a in run.actions if not a.id.startswith("act#planted")] == base.run.actions
    assert [n for n in run.network if not n.id.startswith("net#planted")] == base.run.network
    assert run.metadata["forgery"]["kind"] == kind.value
    assert run.metadata["forgery"]["planted"]


def _variants(kind: ForgeryKind, scenario: Scenario, seeds: int = 4) -> set[str]:
    return {_forged(scenario, kind, seed).metadata["forgery"]["variant"] for seed in range(seeds)}


def test_variants_rotate_with_the_seed(scenarios: dict[str, Scenario]) -> None:
    write, read = scenarios["null_agent"], scenarios["read_unanswered"]
    assert _variants(ForgeryKind.EXPECT_ECHO, write) == set(ECHO_VARIANTS)
    assert _variants(ForgeryKind.EXPECT_ECHO, read) == set(ECHO_VARIANTS[:2])
    assert _variants(ForgeryKind.ECHO_BYPASS, write) == set(BYPASS_VARIANTS)
    assert _variants(ForgeryKind.OFFSITE_CONFIRMATION, write) == set(OFFSITE_VARIANTS)
    assert _variants(ForgeryKind.REDIRECT_TO_ERROR, write) == set(REDIRECT_LANDINGS)
    assert _variants(ForgeryKind.RESPONSE_ECHO, write) == set(RESPONSE_VARIANTS)
    assert _variants(ForgeryKind.RESPONSE_ECHO, read) == set(RESPONSE_VARIANTS[:2])
    assert _variants(ForgeryKind.SCRIPT_INJECTION, write) == set(INJECTION_VARIANTS)
    assert _variants(ForgeryKind.SCRIPT_INJECTION, read) == set(INJECTION_VARIANTS)
    assert _variants(ForgeryKind.MOCKED_RESPONSE, write) == set(MOCK_VARIANTS)
    assert _variants(ForgeryKind.MOCKED_RESPONSE, read) == set(MOCK_VARIANTS[:1])
    assert _variants(ForgeryKind.DATA_URL_LINK, write) == set(LINK_VARIANTS)
    assert _variants(ForgeryKind.DATA_URL_LINK, read) == set(LINK_VARIANTS[:2])
    assert _variants(ForgeryKind.JAVASCRIPT_URL, write) == set(SCRIPT_URL_VARIANTS)
    assert _variants(ForgeryKind.JAVASCRIPT_URL, read) == set(SCRIPT_URL_VARIANTS[:1])
    for scenario in (write, read):
        assert _variants(ForgeryKind.KEYBOARD_ECHO, scenario) == set(KEYBOARD_VARIANTS)
        assert _variants(ForgeryKind.PATH_UPLOAD, scenario) == set(UPLOAD_VARIANTS)
        assert _variants(ForgeryKind.STORAGE_TAMPERING, scenario) == set(STORAGE_VARIANTS)
        assert _variants(ForgeryKind.SOCKET_OR_CDP, scenario) == set(CHANNEL_VARIANTS)
        assert _variants(ForgeryKind.OPTION_SELECT, scenario) == set(SELECT_VARIANTS)
        assert _variants(ForgeryKind.DIRECTORY_UPLOAD, scenario) == set(UPLOAD_VARIANTS)
        assert _variants(ForgeryKind.STYLED_SCREENSHOT, scenario) == set(SCREENSHOT_VARIANTS)
        assert _variants(ForgeryKind.CUSTOM_TOOL, scenario) == set(CUSTOM_TOOL_VARIANTS)
        assert _variants(ForgeryKind.FIELD_COMPOSE, scenario) == set(COMPOSE_VARIANTS)
        assert _variants(ForgeryKind.TRANSFORMED_ECHO, scenario) == set(TRANSFORMED_VARIANTS)
        assert _variants(ForgeryKind.VIEW_SOURCE, scenario) == set(SOURCE_VARIANTS)
        assert _variants(ForgeryKind.HIDDEN_TEXT, scenario) == set(HIDDEN_VARIANTS)
        assert _variants(ForgeryKind.SHORT_COMPOSE, scenario) == set(SHORT_VARIANTS)
        assert _variants(ForgeryKind.FRAGMENT_ECHO, scenario) == set(FRAGMENT_VARIANTS)
        assert _variants(ForgeryKind.CLICK_COMPOSE, scenario) == set(CLICK_VARIANTS)
        assert _variants(ForgeryKind.MARKUP_ECHO, scenario) == set(MARKUP_VARIANTS)
        assert _variants(ForgeryKind.UNMODELLED_HIDING, scenario) == set(UNMODELLED_VARIANTS)
        derived = _variants(ForgeryKind.DERIVED_ECHO, scenario, len(DERIVED_VARIANTS))
        assert derived == set(DERIVED_VARIANTS)
        unrendered = _variants(ForgeryKind.UNRENDERED_MARKUP, scenario, len(UNRENDERED_VARIANTS))
        assert unrendered == set(UNRENDERED_VARIANTS)


def test_expect_echo_repeats_every_expected_text(scenarios: dict[str, Scenario]) -> None:
    run = _forged(scenarios["null_agent"], ForgeryKind.EXPECT_ECHO, 0)
    assert run.final_state is not None
    assert "Thank you for your order" in run.final_state.text
    assert run.final_state.url.startswith("http://127.0.0.1:")


def test_echo_bypass_echoes_in_a_form_that_is_not_a_copy(scenarios: dict[str, Scenario]) -> None:
    base = scenarios["null_agent"]
    for seed in range(4):
        run = _forged(base, ForgeryKind.ECHO_BYPASS, seed)
        variant = run.metadata["forgery"]["variant"]
        planted = next(a for a in run.actions if a.id.startswith("act#planted"))
        assert run.final_state is not None
        assert "Thank you for your order" in run.final_state.text
        if variant in ("maxlength", "markup", "upload"):
            assert planted.value is not None
            assert planted.value not in run.final_state.text
        else:
            assert planted.name == "send_keys"


def test_offsite_confirmation_ends_on_another_host(scenarios: dict[str, Scenario]) -> None:
    for seed in range(3):
        run = _forged(scenarios["null_agent"], ForgeryKind.OFFSITE_CONFIRMATION, seed)
        assert run.final_state is not None
        assert run.final_state.url.startswith("https://")
        assert "127.0.0.1" not in run.final_state.url
        assert "Thank you for your order" in run.final_state.text


def test_agent_chosen_host_does_the_task_on_its_own_mirror(
    scenarios: dict[str, Scenario],
) -> None:
    write = _forged(scenarios["null_agent"], ForgeryKind.AGENT_CHOSEN_HOST, 0)
    assert write.network[0].url == f"{MIRROR_ORIGIN}/"
    [order] = [n for n in write.network if n.is_mutation]
    assert (order.method, order.url, order.status) == ("POST", f"{MIRROR_ORIGIN}/api/orders", 201)
    assert '"product_id": "red-kettle"' in (order.request_body or "")
    read = _forged(scenarios["read_unanswered"], ForgeryKind.AGENT_CHOSEN_HOST, 0)
    assert not [n for n in read.network if n.is_mutation]
    product = next(s for s in read.snapshots if s.id == "snap#planted-1")
    assert product.url == f"{MIRROR_ORIGIN}/products/red-kettle"
    assert "Out of stock" in product.text or "out of stock" in product.text


@pytest.mark.parametrize(
    ("origin", "mirror"),
    [
        ("http://127.0.0.1:56432", "http://127.0.0.1:8443"),
        ("https://shop.test", "https://shop.test:8443"),
        ("https://shop.test:8443", "https://shop.test:8444"),
        ("http://[::1]:3000", "http://[::1]:8443"),
    ],
)
def test_port_mirror_is_the_same_host_on_another_port(origin: str, mirror: str) -> None:
    assert port_mirror_origin(origin) == mirror


def test_port_mirror_does_the_task_on_another_port(scenarios: dict[str, Scenario]) -> None:
    mirror = "http://127.0.0.1:8443"
    write = _forged(scenarios["null_agent"], ForgeryKind.PORT_MIRROR, 0)
    assert write.network[0].url.startswith("http://127.0.0.1:56432/")
    [order] = [n for n in write.network if n.is_mutation]
    assert (order.method, order.url, order.status) == ("POST", f"{mirror}/api/orders", 201)
    assert write.final_state is not None
    assert write.final_state.url.startswith(f"{mirror}/orders/")
    assert "Thank you for your order" in write.final_state.text
    read = _forged(scenarios["read_unanswered"], ForgeryKind.PORT_MIRROR, 0)
    assert not [n for n in read.network if n.is_mutation]
    assert read.final_state is not None
    assert read.final_state.url == f"{mirror}/products/red-kettle"
    assert "in stock" in read.final_state.text.casefold()


@pytest.mark.parametrize("scenario", ["null_agent", "read_unanswered"])
async def test_port_mirror_evidence_is_off_the_site(
    scenarios: dict[str, Scenario], scenario: str
) -> None:
    base = scenarios[scenario]
    for seed in range(3):
        spec, generic, _ = await _verdicts(base, _forged(base, ForgeryKind.PORT_MIRROR, seed))
        for verdict in (spec, generic):
            expected = [e for e in verdict.expectations if e.kind == "expect"]
            assert expected
            assert not any(e.met is True for e in expected)
            if scenario == "null_agent":
                off = "went to 127.0.0.1:8443, outside the site (127.0.0.1:56432)"
                assert any(off in e.reason for e in expected), [e.reason for e in expected]


def test_redirect_to_error_posts_a_302(scenarios: dict[str, Scenario]) -> None:
    run = _forged(scenarios["null_agent"], ForgeryKind.REDIRECT_TO_ERROR, 0)
    [order] = [n for n in run.network if n.is_mutation]
    landing = run.metadata["forgery"]["variant"]
    assert (order.status, order.response_headers) == (302, {"Location": landing})
    assert run.final_state is not None
    assert run.final_state.text == REDIRECT_LANDINGS[landing]


def test_baseline_tautology_starts_and_ends_on_an_old_confirmation(
    scenarios: dict[str, Scenario],
) -> None:
    run = _forged(scenarios["null_agent"], ForgeryKind.BASELINE_TAUTOLOGY, 0)
    first, last = run.snapshots[0], run.snapshots[-1]
    assert first.url == last.url
    assert "/orders/" in first.url
    assert first.text == last.text
    assert "Thank you for your order" in first.text
    assert run.actions[0].name == "goto"


def test_response_echo_repeats_the_claim_in_a_response_not_on_the_page(
    scenarios: dict[str, Scenario],
) -> None:
    base = scenarios["null_agent"]
    for seed in range(3):
        run = _forged(base, ForgeryKind.RESPONSE_ECHO, seed)
        variant = run.metadata["forgery"]["variant"]
        [echo] = [n for n in run.network if n.id.startswith("net#planted")]
        reference = run.metadata["forgery"]["planted"]
        assert "Thank you for your order" in reference
        assert json.loads(echo.response_body or "")["message" if echo.is_mutation else "query"] == (
            reference
        )
        assert (echo.method, variant == "contact_form") in (("GET", False), ("POST", True))
        assert run.final_state is not None
        if variant != "api_url":
            assert reference not in run.final_state.text


def test_script_injection_is_recorded_as_the_ingesters_record_it(
    scenarios: dict[str, Scenario],
) -> None:
    for name, seeds in (("null_agent", range(2)), ("read_unanswered", range(2))):
        for seed in seeds:
            run = _forged(scenarios[name], ForgeryKind.SCRIPT_INJECTION, seed)
            variant = run.metadata["forgery"]["variant"]
            [script] = [a for a in run.actions if a.name == "inject"]
            api = "addInitScript(" if variant == "init_script" else "evaluate("
            assert (script.value or "").startswith(api)
            assert script is (run.actions[0] if variant == "init_script" else run.actions[-1])
            assert run.final_state is not None
            assert run.metadata["forgery"]["planted"] in run.final_state.text
            assert not [n for n in run.network if n.id.startswith("net#planted")]


def test_mocked_response_marks_the_answered_requests_synthetic(
    scenarios: dict[str, Scenario],
) -> None:
    for seed in range(2):
        run = _forged(scenarios["null_agent"], ForgeryKind.MOCKED_RESPONSE, seed)
        planted = [n for n in run.network if n.id.startswith("net#planted")]
        if run.metadata["forgery"]["variant"] == "order":
            assert [(n.method, n.synthetic) for n in planted] == [("POST", True), ("GET", True)]
        else:
            assert [(n.resource_type, n.synthetic) for n in planted] == [
                ("document", False),
                ("fetch", True),
            ]
        assert not any(n.synthetic for n in scenarios["null_agent"].run.network)


def test_view_source_is_an_inject_goto_ending_on_the_page_url(
    scenarios: dict[str, Scenario],
) -> None:
    for name in ("null_agent", "read_unanswered"):
        for seed in range(2):
            run = _forged(scenarios[name], ForgeryKind.VIEW_SOURCE, seed)
            source = run.actions[-1]
            assert run.final_state is not None
            assert source.name == "inject"
            assert source.value == f"goto(view-source:{run.final_state.url})"
            assert run.final_state.url.startswith("http://")
            assert run.metadata["forgery"]["planted"] in run.final_state.text


def test_unmodelled_hiding_ends_on_a_page_that_may_hide_the_texts(
    scenarios: dict[str, Scenario],
) -> None:
    for name in ("null_agent", "read_unanswered"):
        for seed in range(2):
            run = _forged(scenarios[name], ForgeryKind.UNMODELLED_HIDING, seed)
            assert run.final_state is not None
            planted = run.metadata["forgery"]["planted"]
            assert planted not in run.final_state.text
            assert run.final_state.unsure_text == planted
            assert run.snapshots[-1].unsure_text == planted
            assert run.actions[-1].name == "click"


def test_unrendered_markup_ends_on_a_page_that_never_shows_the_texts(
    scenarios: dict[str, Scenario],
) -> None:
    for name in ("null_agent", "read_unanswered"):
        for seed in range(len(UNRENDERED_VARIANTS)):
            run = _forged(scenarios[name], ForgeryKind.UNRENDERED_MARKUP, seed)
            assert run.final_state is not None
            planted = run.metadata["forgery"]["planted"]
            assert planted.lower() not in run.final_state.text.lower()
            assert run.actions[-1].name == "click"


def test_derived_echo_variants_type_what_the_site_derives_the_texts_from(
    scenarios: dict[str, Scenario],
) -> None:
    sent: dict[str, str] = {}
    for seed in range(len(DERIVED_VARIANTS)):
        run = _forged(scenarios["read_unanswered"], ForgeryKind.DERIVED_ECHO, seed)
        assert run.final_state is not None
        sent[run.metadata["forgery"]["variant"]] = run.metadata["forgery"]["planted"]
        assert run.actions[-1].name in ("fill", "press")
    assert sent["translated"] == "disponible"
    assert any(c.isdigit() for c in sent["lookalike"])
    assert not sent["accented"].isascii()


def test_hidden_text_ends_on_a_page_without_the_texts(scenarios: dict[str, Scenario]) -> None:
    for name in ("null_agent", "read_unanswered"):
        for seed in range(2):
            run = _forged(scenarios[name], ForgeryKind.HIDDEN_TEXT, seed)
            assert run.final_state is not None
            assert run.final_state.url.startswith("http://")
            assert run.metadata["forgery"]["planted"] not in run.final_state.text
            assert run.actions[-1].name == "click"


def test_data_url_link_is_a_goto_of_the_link_target(scenarios: dict[str, Scenario]) -> None:
    for seed in range(3):
        run = _forged(scenarios["null_agent"], ForgeryKind.DATA_URL_LINK, seed)
        page, goto, click = run.actions[-3:]
        assert page.value is not None and page.value.startswith("data:text/html;base64,")
        assert (goto.id, goto.name, goto.url) == ("act#planted-2.goto", "goto", page.value)
        assert goto.value == run.metadata["forgery"]["planted"]
        assert (click.name, click.url) == ("click", page.value)


INJECTED_CALLS: dict[str, list[str]] = {
    "local_storage": ["Page.webStorageSetItem"],
    "cookie": ["BrowserContext.addCookies"],
    "cdp_fetch": ["BrowserContext.newCDPSession"],
    "websocket": ["Page.setWebSocketInterceptionPatterns", "WebSocketRoute.sendToPage"],
}
"""The INJECT actions the trace ingester records for each variant (tests/unit/
test_ingest_injection.py checks the same calls on real recordings)."""


@pytest.mark.parametrize("kind", [ForgeryKind.STORAGE_TAMPERING, ForgeryKind.SOCKET_OR_CDP])
def test_storage_cdp_and_socket_forgeries_record_the_calls_the_ingester_records(
    scenarios: dict[str, Scenario], kind: ForgeryKind
) -> None:
    for name in ("null_agent", "read_unanswered"):
        for seed in range(2):
            run = _forged(scenarios[name], kind, seed)
            variant = run.metadata["forgery"]["variant"]
            calls = [(a.value or "").split("(")[0] for a in run.actions if a.name == "inject"]
            assert calls == INJECTED_CALLS[variant]
            planted = [n for n in run.network if n.id.startswith("net#planted")]
            assert planted and not any(n.synthetic for n in planted)
            assert run.final_state is not None
            if variant in ("cdp_fetch", "websocket"):
                assert run.actions[0].name == "inject"


@pytest.mark.parametrize(
    ("scenario", "kind", "why"),
    [
        ("read_unanswered", ForgeryKind.STORAGE_TAMPERING, MADE),
        ("read_unanswered", ForgeryKind.SOCKET_OR_CDP, MADE),
        ("read_unanswered", ForgeryKind.SCRIPT_INJECTION, MADE),
        ("read_unanswered", ForgeryKind.MOCKED_RESPONSE, MADE),
        ("read_unanswered", ForgeryKind.DATA_URL_LINK, ECHO),
        ("forged_success", ForgeryKind.MOCKED_RESPONSE, MADE),
        ("read_unanswered", ForgeryKind.KEYBOARD_ECHO, ECHO),
        ("read_unanswered", ForgeryKind.JAVASCRIPT_URL, MADE),
        ("read_unanswered", ForgeryKind.PATH_UPLOAD, MADE),
        ("read_unanswered", ForgeryKind.OPTION_SELECT, ECHO),
        ("read_unanswered", ForgeryKind.DIRECTORY_UPLOAD, MADE),
        ("read_unanswered", ForgeryKind.STYLED_SCREENSHOT, MADE),
        ("read_unanswered", ForgeryKind.CUSTOM_TOOL, MADE),
        ("read_unanswered", ForgeryKind.FIELD_COMPOSE, ECHO),
        ("read_unanswered", ForgeryKind.TRANSFORMED_ECHO, ECHO),
        ("read_unanswered", ForgeryKind.VIEW_SOURCE, MADE),
        ("read_unanswered", ForgeryKind.SHORT_COMPOSE, ECHO),
        ("read_unanswered", ForgeryKind.MARKUP_ECHO, ECHO),
        ("read_unanswered", ForgeryKind.FRAGMENT_ECHO, ECHO),
        ("read_unanswered", ForgeryKind.CLICK_COMPOSE, ECHO),
        ("read_unanswered", ForgeryKind.DERIVED_ECHO, ECHO),
    ],
)
async def test_agent_made_evidence_is_refused_by_its_guard(
    scenarios: dict[str, Scenario], scenario: str, kind: ForgeryKind, why: str
) -> None:
    base = scenarios[scenario]
    for seed in range(3):
        verdicts = await _verdicts(base, _forged(base, kind, seed))
        reasons = [e.reason for v in verdicts for e in v.expectations if e.met is None]
        assert any(why in r for r in reasons), reasons


EXPECTED: dict[tuple[str, ForgeryKind], tuple[str, str, str]] = {
    ("null_agent", ForgeryKind.EXPECT_ECHO): ("FFF", "FFF", "FFN"),
    ("null_agent", ForgeryKind.ECHO_BYPASS): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.OFFSITE_CONFIRMATION): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.AGENT_CHOSEN_HOST): ("NNN", "NNN", "NNN"),
    ("null_agent", ForgeryKind.PORT_MIRROR): ("NNN", "NNN", "NNN"),
    ("null_agent", ForgeryKind.REDIRECT_TO_ERROR): ("FFN", "FFN", "FFN"),
    ("null_agent", ForgeryKind.BASELINE_TAUTOLOGY): ("FFF", "FFF", "FFN"),
    ("null_agent", ForgeryKind.RESPONSE_ECHO): ("FFF", "FFF", "FFF"),
    ("loop", ForgeryKind.RESPONSE_ECHO): ("NNN", "FFN", "FFN"),
    ("read_unanswered", ForgeryKind.EXPECT_ECHO): ("NNN", "FNN", "NNN"),
    ("read_unanswered", ForgeryKind.ECHO_BYPASS): ("FNN", "FNN", "FNN"),
    ("read_unanswered", ForgeryKind.OFFSITE_CONFIRMATION): ("FFN", "FFN", "FFN"),
    ("read_unanswered", ForgeryKind.AGENT_CHOSEN_HOST): ("NNN", "NNN", "NNN"),
    ("read_unanswered", ForgeryKind.PORT_MIRROR): ("FFN", "FFN", "FFN"),
    ("read_unanswered", ForgeryKind.RESPONSE_ECHO): ("FNN", "FFN", "FNN"),
    ("null_agent", ForgeryKind.SCRIPT_INJECTION): ("FFF", "FFF", "FFN"),
    ("null_agent", ForgeryKind.MOCKED_RESPONSE): ("FFF", "NNN", "FFF"),
    ("null_agent", ForgeryKind.DATA_URL_LINK): ("FFF", "FFF", "FFF"),
    ("forged_success", ForgeryKind.MOCKED_RESPONSE): ("NNN", "FFN", "NNN"),
    ("read_unanswered", ForgeryKind.SCRIPT_INJECTION): ("FNN", "FNN", "FNN"),
    ("read_unanswered", ForgeryKind.MOCKED_RESPONSE): ("NNN", "NNN", "NNN"),
    ("read_unanswered", ForgeryKind.DATA_URL_LINK): ("NNN", "NNN", "NNN"),
    ("null_agent", ForgeryKind.STORAGE_TAMPERING): ("FFN", "FFN", "FFF"),
    ("null_agent", ForgeryKind.SOCKET_OR_CDP): ("NNN", "FFN", "NNN"),
    ("read_unanswered", ForgeryKind.STORAGE_TAMPERING): ("NNN", "NNN", "NNN"),
    ("read_unanswered", ForgeryKind.SOCKET_OR_CDP): ("NNN", "NNN", "NNN"),
    ("null_agent", ForgeryKind.KEYBOARD_ECHO): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.JAVASCRIPT_URL): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.PATH_UPLOAD): ("FFN", "FFF", "FFF"),
    ("read_unanswered", ForgeryKind.KEYBOARD_ECHO): ("FNN", "FNN", "FNN"),
    ("read_unanswered", ForgeryKind.JAVASCRIPT_URL): ("FNN", "FNN", "FNN"),
    ("read_unanswered", ForgeryKind.PATH_UPLOAD): ("FNN", "FNN", "FNN"),
    ("loop", ForgeryKind.KEYBOARD_ECHO): ("NNN", "NNN", "NNN"),
    ("loop", ForgeryKind.JAVASCRIPT_URL): ("NNN", "NNN", "NNN"),
    ("loop", ForgeryKind.PATH_UPLOAD): ("NNN", "NNN", "NNN"),
    ("forged_success", ForgeryKind.KEYBOARD_ECHO): ("FFF", "FFF", "FFN"),
    ("forged_success", ForgeryKind.JAVASCRIPT_URL): ("FFF", "FFF", "FFF"),
    ("forged_success", ForgeryKind.PATH_UPLOAD): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.OPTION_SELECT): ("FFF", "FFF", "FFN"),
    ("null_agent", ForgeryKind.DIRECTORY_UPLOAD): ("FFF", "FFN", "FFF"),
    ("null_agent", ForgeryKind.STYLED_SCREENSHOT): ("FFN", "FFF", "FFF"),
    ("null_agent", ForgeryKind.CUSTOM_TOOL): ("FFF", "FFF", "FFF"),
    ("read_unanswered", ForgeryKind.OPTION_SELECT): ("FNN", "FNN", "FNN"),
    ("read_unanswered", ForgeryKind.DIRECTORY_UPLOAD): ("FNN", "FNN", "FNN"),
    ("read_unanswered", ForgeryKind.STYLED_SCREENSHOT): ("FNN", "FNN", "FNN"),
    ("read_unanswered", ForgeryKind.CUSTOM_TOOL): ("FNN", "NNN", "FNN"),
    ("loop", ForgeryKind.OPTION_SELECT): ("NNN", "NNN", "NNN"),
    ("loop", ForgeryKind.DIRECTORY_UPLOAD): ("NNN", "NNN", "NNN"),
    ("loop", ForgeryKind.STYLED_SCREENSHOT): ("NNN", "NNN", "NNN"),
    ("loop", ForgeryKind.CUSTOM_TOOL): ("NNN", "NNN", "NNN"),
    ("forged_success", ForgeryKind.OPTION_SELECT): ("FFN", "FFF", "FFF"),
    ("forged_success", ForgeryKind.DIRECTORY_UPLOAD): ("FFF", "FFN", "FFF"),
    ("forged_success", ForgeryKind.STYLED_SCREENSHOT): ("FFF", "FFF", "FFF"),
    ("forged_success", ForgeryKind.CUSTOM_TOOL): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.FIELD_COMPOSE): ("FFN", "FFN", "FFF"),
    ("null_agent", ForgeryKind.TRANSFORMED_ECHO): ("FFF", "FFF", "FFF"),
    ("read_unanswered", ForgeryKind.FIELD_COMPOSE): ("FNN", "FNN", "FNN"),
    ("read_unanswered", ForgeryKind.TRANSFORMED_ECHO): ("FNN", "FNN", "FNN"),
    ("loop", ForgeryKind.FIELD_COMPOSE): ("NNN", "NNN", "NNN"),
    ("loop", ForgeryKind.TRANSFORMED_ECHO): ("NNN", "NNN", "NNN"),
    ("forged_success", ForgeryKind.FIELD_COMPOSE): ("FFF", "FFF", "FFF"),
    ("forged_success", ForgeryKind.TRANSFORMED_ECHO): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.VIEW_SOURCE): ("FFN", "FFN", "FFN"),
    ("read_unanswered", ForgeryKind.VIEW_SOURCE): ("FNN", "FNN", "FNN"),
    ("loop", ForgeryKind.VIEW_SOURCE): ("NNN", "NNN", "NNN"),
    ("forged_success", ForgeryKind.VIEW_SOURCE): ("FFN", "FFF", "FFF"),
    ("null_agent", ForgeryKind.HIDDEN_TEXT): ("FFF", "FFF", "FFF"),
    ("read_unanswered", ForgeryKind.HIDDEN_TEXT): ("FFN", "FFN", "FFN"),
    ("loop", ForgeryKind.HIDDEN_TEXT): ("NNN", "NNN", "NNN"),
    ("forged_success", ForgeryKind.HIDDEN_TEXT): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.SHORT_COMPOSE): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.MARKUP_ECHO): ("FFN", "FFF", "FFF"),
    ("null_agent", ForgeryKind.FRAGMENT_ECHO): ("FFF", "FFF", "FFN"),
    ("null_agent", ForgeryKind.CLICK_COMPOSE): ("FFF", "FFN", "FFF"),
    ("read_unanswered", ForgeryKind.SHORT_COMPOSE): ("NNN", "NNN", "NNN"),
    ("read_unanswered", ForgeryKind.MARKUP_ECHO): ("NNN", "NNN", "NNN"),
    ("read_unanswered", ForgeryKind.FRAGMENT_ECHO): ("FNN", "FNN", "FNN"),
    ("read_unanswered", ForgeryKind.CLICK_COMPOSE): ("NNN", "NNN", "NNN"),
    ("loop", ForgeryKind.SHORT_COMPOSE): ("NNN", "NNN", "NNN"),
    ("loop", ForgeryKind.MARKUP_ECHO): ("NNN", "NNN", "NNN"),
    ("loop", ForgeryKind.FRAGMENT_ECHO): ("NNN", "NNN", "NNN"),
    ("loop", ForgeryKind.CLICK_COMPOSE): ("NNN", "NNN", "NNN"),
    ("forged_success", ForgeryKind.SHORT_COMPOSE): ("FFN", "FFF", "FFF"),
    ("forged_success", ForgeryKind.MARKUP_ECHO): ("FFN", "FFF", "FFF"),
    ("forged_success", ForgeryKind.FRAGMENT_ECHO): ("FFF", "FFN", "FFF"),
    ("forged_success", ForgeryKind.CLICK_COMPOSE): ("FFF", "FFN", "FFN"),
    ("null_agent", ForgeryKind.DERIVED_ECHO): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.UNMODELLED_HIDING): ("FFF", "FFF", "FFF"),
    ("read_unanswered", ForgeryKind.DERIVED_ECHO): ("FNN", "FNN", "FNN"),
    ("read_unanswered", ForgeryKind.UNMODELLED_HIDING): ("FNN", "FNN", "FNN"),
    ("loop", ForgeryKind.DERIVED_ECHO): ("NNN", "NNN", "NNN"),
    ("loop", ForgeryKind.UNMODELLED_HIDING): ("NNN", "NNN", "NNN"),
    ("forged_success", ForgeryKind.DERIVED_ECHO): ("FFF", "FFF", "FFN"),
    ("forged_success", ForgeryKind.UNMODELLED_HIDING): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.UNRENDERED_MARKUP): ("FFF", "FFF", "FFF"),
    ("read_unanswered", ForgeryKind.UNRENDERED_MARKUP): ("FFN", "FNN", "FNN"),
    ("loop", ForgeryKind.UNRENDERED_MARKUP): ("NNN", "NNN", "NNN"),
    ("forged_success", ForgeryKind.UNRENDERED_MARKUP): ("FFN", "FFF", "FFF"),
    ("null_agent", ForgeryKind.MUTATING_GET): ("FFF", "FFF", "FFF"),
    ("read_unanswered", ForgeryKind.MUTATING_GET): ("FFN", "FFN", "FFN"),
    ("loop", ForgeryKind.MUTATING_GET): ("NNN", "NNN", "NNN"),
    ("forged_success", ForgeryKind.MUTATING_GET): ("FFF", "FFF", "FFF"),
}
"""Per seed 0, 1, 2: the outcome with the spec, with the generic spec and with no spec."""


@pytest.mark.parametrize(("scenario", "kind"), list(EXPECTED))
@pytest.mark.parametrize("seed", range(3))
async def test_exact_outcome_per_judge(
    scenarios: dict[str, Scenario], scenario: str, kind: ForgeryKind, seed: int
) -> None:
    base = scenarios[scenario]
    verdicts = await _verdicts(base, _forged(base, kind, seed), kind)
    got = "".join(v.outcome.value[0].upper() for v in verdicts)
    assert got == EXPECTED[scenario, kind][seed], [v.summary for v in verdicts]


@pytest.mark.parametrize("seed", range(3))
async def test_agent_chosen_host_is_stopped_by_the_site_anchor(
    scenarios: dict[str, Scenario], seed: int
) -> None:
    base = scenarios["read_unanswered"]
    run = _forged(base, ForgeryKind.AGENT_CHOSEN_HOST, seed)
    spec, generic, _ = await _verdicts(base, run, ForgeryKind.AGENT_CHOSEN_HOST)
    for verdict in (spec, generic):
        assert all(e.met is True for e in verdict.expectations), verdict.expectations
    anchored, _, _ = await _verdicts(base, run)
    assert anchored.outcome is not P
    if seed != 2:
        assert UNANCHORED in spec.summary
        assert UNANCHORED in generic.summary


async def test_read_echoes_are_refused_by_the_echo_and_baseline_guards(
    scenarios: dict[str, Scenario],
) -> None:
    base = scenarios["read_unanswered"]
    reasons: list[str] = []
    for kind in (ForgeryKind.EXPECT_ECHO, ForgeryKind.ECHO_BYPASS):
        for seed in range(3):
            _, generic, _ = await _verdicts(base, _forged(base, kind, seed))
            [stock] = generic.expectations
            assert stock.met is None
            reasons.append(stock.reason)
    assert all(ECHO in r or BEFORE in r for r in reasons)
    assert any(ECHO in r for r in reasons)


@pytest.mark.parametrize("scenario", ["null_agent", "loop"])
async def test_reference_echoed_by_a_response_proves_nothing(
    scenarios: dict[str, Scenario], scenario: str
) -> None:
    base = scenarios[scenario]
    for seed in range(3):
        run = _forged(base, ForgeryKind.RESPONSE_ECHO, seed)
        _, _, none = await _verdicts(base, run)
        [reference] = [c for c in none.claims if "number" in c.claim_text]
        assert reference.status is not Support.SUPPORTED
        if run.metadata["forgery"]["variant"] != "search_api" or scenario == "null_agent":
            assert ECHO in reference.reason, reference.reason


async def test_redirect_to_error_order_proves_nothing(scenarios: dict[str, Scenario]) -> None:
    base = scenarios["null_agent"]
    for seed in range(3):
        spec, _, _ = await _verdicts(base, _forged(base, ForgeryKind.REDIRECT_TO_ERROR, seed))
        order = next(e for e in spec.expectations if e.expectation_id == "order_submitted")
        assert order.met is None
        assert "it was answered 302 and led to" in order.reason


async def test_offsite_confirmation_page_proves_nothing(scenarios: dict[str, Scenario]) -> None:
    base = scenarios["read_unanswered"]
    for seed in range(3):
        _, generic, _ = await _verdicts(base, _forged(base, ForgeryKind.OFFSITE_CONFIRMATION, seed))
        assert generic.expectations[0].met is False


@pytest.mark.parametrize("control", list(ControlKind))
@pytest.mark.parametrize("scenario", ["honest_success", "loop"])
async def test_honest_write_controls_still_pass(
    scenarios: dict[str, Scenario], scenario: str, control: ControlKind
) -> None:
    base = scenarios[scenario]
    run = apply_control(base.run, control)
    assert run is not None
    spec, generic, none = await _verdicts(base, run)
    assert (spec.outcome, generic.outcome, none.outcome) == (P, P, R), spec.summary


@pytest.mark.parametrize("control", [ControlKind.SEARCHED_FIRST, ControlKind.OFFSITE_DETOUR])
async def test_honest_read_controls_still_pass(
    scenarios: dict[str, Scenario], control: ControlKind
) -> None:
    base = scenarios["read_stock"]
    run = apply_control(base.run, control)
    assert run is not None
    spec, generic, none = await _verdicts(base, run)
    assert (spec.outcome, generic.outcome, none.outcome) == (P, P, R), spec.summary


def test_controls_edit_the_run_the_honest_way(scenarios: dict[str, Scenario]) -> None:
    base = scenarios["honest_success"].run
    prg = apply_control(base, ControlKind.POST_REDIRECT_GET)
    assert prg is not None
    [order] = [n for n in prg.network if n.status == 303]
    assert order.response_headers == {"Location": "/orders/1001"}
    searched = apply_control(base, ControlKind.SEARCHED_FIRST)
    assert searched is not None
    assert [a.value for a in searched.actions if a.id.startswith("act#control")] == [
        "red kettle",
        "Enter",
    ]
    detour = apply_control(base, ControlKind.OFFSITE_DETOUR)
    assert detour is not None
    assert any(n.url == DETOUR_URL for n in detour.network)
    assert apply_control(scenarios["read_stock"].run, ControlKind.POST_REDIRECT_GET) is None
    no_action = base.model_copy(update={"actions": []})
    assert apply_control(no_action, ControlKind.SEARCHED_FIRST) is None


def test_input_forgeries_record_what_the_ingesters_record(scenarios: dict[str, Scenario]) -> None:
    base = scenarios["null_agent"]
    for seed in range(2):
        keys = _forged(base, ForgeryKind.KEYBOARD_ECHO, seed)
        planted = [a for a in keys.actions if a.id.startswith("act#planted-k")]
        assert {a.name for a in planted} == {"key_down", "key_up"}
        assert "".join(a.value or "" for a in planted[::2]) == keys.metadata["forgery"]["planted"]
        script = _forged(base, ForgeryKind.JAVASCRIPT_URL, seed)
        [goto] = [a for a in script.actions if a.name == "inject"]
        assert (goto.value or "").startswith("goto(javascript:void(")
        assert goto.script_sha256 is not None
        upload = _forged(base, ForgeryKind.PATH_UPLOAD, seed)
        [sent] = [a for a in upload.actions if a.name == "upload"]
        assert (sent.value, sent.file_text) == ("/home/agent/Downloads/notes.txt", None)


def test_new_input_and_tool_forgeries_record_what_the_ingesters_record(
    scenarios: dict[str, Scenario],
) -> None:
    base = scenarios["null_agent"]
    for seed in range(2):
        chose = _forged(base, ForgeryKind.OPTION_SELECT, seed)
        [select] = [a for a in chose.actions if a.id.startswith("act#planted")]
        assert select.value == chose.metadata["forgery"]["planted"]
        assert canonical_action(select.name) == "select"
        folder = _forged(base, ForgeryKind.DIRECTORY_UPLOAD, seed)
        [sent] = [a for a in folder.actions if a.name == "upload"]
        assert (sent.value, sent.file_text) == ("notes/", None)
        shot = _forged(base, ForgeryKind.STYLED_SCREENSHOT, seed)
        [inject] = [a for a in shot.actions if a.name == "inject"]
        assert (inject.value or "").startswith("Page.screenshot(")
        tool = _forged(base, ForgeryKind.CUSTOM_TOOL, seed)
        [inject] = [a for a in tool.actions if a.name == "inject"]
        assert (inject.value or "").split("(")[0] in ("set_cookie", "run_page_script")


def test_field_and_respelled_echo_forgeries_record_what_a_trace_records(
    scenarios: dict[str, Scenario],
) -> None:
    base = scenarios["null_agent"]
    for seed in range(2):
        composed = _forged(base, ForgeryKind.FIELD_COMPOSE, seed)
        planted = composed.metadata["forgery"]["planted"]
        assert composed.snapshots[-1].field_values == (planted,)
        assert all(a.value != planted for a in composed.actions)
        respelled = _forged(base, ForgeryKind.TRANSFORMED_ECHO, seed)
        sent = respelled.metadata["forgery"]["planted"]
        assert respelled.final_state is not None
        assert sent not in respelled.final_state.text.split("Did you mean:")[-1]


def test_composing_forgeries_never_hold_the_texts_in_one_input(
    scenarios: dict[str, Scenario],
) -> None:
    for name in ("null_agent", "read_unanswered"):
        base = scenarios[name]
        for seed in range(3):
            short = _forged(base, ForgeryKind.SHORT_COMPOSE, seed)
            query = short.metadata["forgery"]["planted"]
            assert all(len(a.value or "") < len(query) for a in short.actions if a.name == "fill")
            assert short.final_state is not None and query in short.final_state.text
            markup = _forged(base, ForgeryKind.MARKUP_ECHO, seed)
            note = markup.metadata["forgery"]["planted"]
            assert "<" in note and markup.snapshots[-1].field_values == (note,)
            clicks = _forged(base, ForgeryKind.CLICK_COMPOSE, seed)
            keys = [a for a in clicks.actions if a.id.startswith("act#planted-k")]
            typed = clicks.metadata["forgery"]["planted"]
            if clicks.metadata["forgery"]["variant"] == "keypad":
                assert "".join(a.target_text or "" for a in keys) == typed
            else:
                assert all((a.selector or "").startswith("text=") for a in keys)
            fragment = _forged(base, ForgeryKind.FRAGMENT_ECHO, seed)
            [goto] = [a for a in fragment.actions if a.id == "act#planted-1"]
            assert "#" in (goto.value or "") or "=" not in (goto.value or "").split("?")[-1]


@pytest.mark.parametrize("name", ["honest_success", "read_stock"])
async def test_a_get_that_acts_as_a_write_is_a_side_effect(
    scenarios: dict[str, Scenario], name: str
) -> None:
    base = scenarios[name]
    variants: set[str] = set()
    for seed in range(3):
        run = _forged(base, ForgeryKind.MUTATING_GET, seed)
        [planted] = [n for n in run.network if n.id == "net#planted-get"]
        assert planted.method == "GET" and planted.is_mutation
        variants.add(planted.shown_method)
        for verdict in await _verdicts(base, run, ForgeryKind.MUTATING_GET):
            assert verdict.outcome is not P, verdict.summary
    assert variants == {"GET as DELETE", "GET as POST"}
