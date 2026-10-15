"""The forgeries for attacks that once produced false PASSes, and their honest controls."""

from __future__ import annotations

from pathlib import Path

import pytest

from evidence_judge.bench import load_scenario
from evidence_judge.bench.dataset import Scenario
from evidence_judge.config import Settings
from evidence_judge.core.evidence import Run
from evidence_judge.core.taskspec import TaskSpec
from evidence_judge.core.verdict import Outcome, RunVerdict
from evidence_judge.engine import Judge, JudgeOptions
from evidence_judge.forgery import ForgeryKind, forge, plant_evidence
from evidence_judge.forgery.controls import DETOUR_URL, ControlKind, apply_control
from evidence_judge.forgery.evidence import (
    BYPASS_VARIANTS,
    ECHO_VARIANTS,
    MIRROR_ORIGIN,
    OFFSITE_VARIANTS,
    REDIRECT_LANDINGS,
    UNANCHORED_KINDS,
    WRITE_ONLY_KINDS,
    is_write_task,
    unanchored,
)

RUNS = Path(__file__).parents[1] / "fixtures" / "runs"
NEW_KINDS = (
    ForgeryKind.EXPECT_ECHO,
    ForgeryKind.ECHO_BYPASS,
    ForgeryKind.OFFSITE_CONFIRMATION,
    ForgeryKind.AGENT_CHOSEN_HOST,
    ForgeryKind.REDIRECT_TO_ERROR,
    ForgeryKind.BASELINE_TAUTOLOGY,
)
F, R, P = Outcome.FAIL, Outcome.NEEDS_REVIEW, Outcome.PASS
ECHO = "echoes the agent's own input"
BEFORE = "already true before the agent acted"
UNANCHORED = "was inferred from the run's first page"


@pytest.fixture(scope="module")
def scenarios() -> dict[str, Scenario]:
    names = ("null_agent", "read_unanswered", "honest_success", "loop", "read_stock")
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


def _variants(kind: ForgeryKind, scenario: Scenario) -> set[str]:
    return {_forged(scenario, kind, seed).metadata["forgery"]["variant"] for seed in range(4)}


def test_variants_rotate_with_the_seed(scenarios: dict[str, Scenario]) -> None:
    write, read = scenarios["null_agent"], scenarios["read_unanswered"]
    assert _variants(ForgeryKind.EXPECT_ECHO, write) == set(ECHO_VARIANTS)
    assert _variants(ForgeryKind.EXPECT_ECHO, read) == set(ECHO_VARIANTS[:2])
    assert _variants(ForgeryKind.ECHO_BYPASS, write) == set(BYPASS_VARIANTS)
    assert _variants(ForgeryKind.OFFSITE_CONFIRMATION, write) == set(OFFSITE_VARIANTS)
    assert _variants(ForgeryKind.REDIRECT_TO_ERROR, write) == set(REDIRECT_LANDINGS)


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


EXPECTED: dict[tuple[str, ForgeryKind], tuple[str, str, str]] = {
    ("null_agent", ForgeryKind.EXPECT_ECHO): ("FFF", "FFF", "FFN"),
    ("null_agent", ForgeryKind.ECHO_BYPASS): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.OFFSITE_CONFIRMATION): ("FFF", "FFF", "FFF"),
    ("null_agent", ForgeryKind.AGENT_CHOSEN_HOST): ("NNN", "NNN", "NNN"),
    ("null_agent", ForgeryKind.REDIRECT_TO_ERROR): ("FFN", "FFN", "FFN"),
    ("null_agent", ForgeryKind.BASELINE_TAUTOLOGY): ("FFF", "FFF", "FFN"),
    ("read_unanswered", ForgeryKind.EXPECT_ECHO): ("NNN", "FNN", "NNN"),
    ("read_unanswered", ForgeryKind.ECHO_BYPASS): ("FNN", "FNN", "FNN"),
    ("read_unanswered", ForgeryKind.OFFSITE_CONFIRMATION): ("FFN", "FFN", "FFN"),
    ("read_unanswered", ForgeryKind.AGENT_CHOSEN_HOST): ("NNN", "NNN", "NNN"),
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
