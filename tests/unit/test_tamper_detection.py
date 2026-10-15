"""Every verdict discloses what its evidence source can show of tampering, and a run can be
required to come from a source that shows it fully."""

from __future__ import annotations

import pytest
from rich.console import Console

from evidence_judge.checks.outcome import REVIEW_SCORE_CAP
from evidence_judge.config import Settings
from evidence_judge.core.evidence import Channel, Run
from evidence_judge.core.verdict import Outcome, TamperLevel
from evidence_judge.engine import Judge, JudgeOptions, tamper_detection
from evidence_judge.report.console import print_verdict
from tests.unit.conftest import honest_run, shop_spec

ANSWER = "I bought the red kettle for $24.00 and placed the order."
SETTINGS = Settings(anthropic_api_key=None, cache_dir=None)
FORGED_HAR_WARNING = "a forged HAR would look the same"


def _from(source: str, *, actions: bool = True) -> Run:
    run = honest_run()
    captured = run.captured if actions else run.captured - {Channel.ACTIONS}
    return run.model_copy(update={"source": source, "captured": captured, "agent_answer": ANSWER})


@pytest.mark.parametrize(
    ("source", "actions", "level"),
    [
        ("playwright-trace", True, TamperLevel.FULL),
        ("playwright-trace+har", True, TamperLevel.FULL),
        ("playwright-trace+har+browser-use", True, TamperLevel.FULL),
        ("playwright-trace", False, TamperLevel.NONE),
        ("browser-use", True, TamperLevel.PARTIAL),
        ("har+browser-use", True, TamperLevel.PARTIAL),
        ("har", False, TamperLevel.NONE),
        ("har", True, TamperLevel.NONE),
        ("synthetic", True, TamperLevel.NONE),
    ],
)
def test_level_follows_the_source(source: str, actions: bool, level: TamperLevel) -> None:
    assert tamper_detection(_from(source, actions=actions)).level is level


async def test_har_only_run_passes_but_says_it_cannot_show_tampering() -> None:
    verdict = await Judge(SETTINGS).judge(_from("har"), JudgeOptions(spec=shop_spec()))
    assert verdict.outcome is Outcome.PASS
    assert verdict.tamper_detection is not None
    assert verdict.tamper_detection.level is TamperLevel.NONE
    assert FORGED_HAR_WARNING in verdict.tamper_detection.reason
    [warning] = [w for w in verdict.warnings if w.startswith("tamper detection is none")]
    assert "(har)" in warning
    assert FORGED_HAR_WARNING in warning


async def test_trace_run_has_no_tamper_warning() -> None:
    verdict = await Judge(SETTINGS).judge(_from("playwright-trace"), JudgeOptions(spec=shop_spec()))
    assert verdict.outcome is Outcome.PASS
    assert verdict.tamper_detection is not None
    assert verdict.tamper_detection.level is TamperLevel.FULL
    assert not any("tamper detection" in w for w in verdict.warnings)


@pytest.mark.parametrize("via", ["option", "spec"])
@pytest.mark.parametrize(("source", "level"), [("har", "none"), ("browser-use", "partial")])
async def test_required_tamper_evidence_caps_other_sources_at_review(
    via: str, source: str, level: str
) -> None:
    spec = shop_spec()
    if via == "spec":
        spec = spec.model_copy(update={"require_tamper_evidence": True})
    options = JudgeOptions(spec=spec, require_tamper_evidence=via == "option")
    verdict = await Judge(SETTINGS).judge(_from(source), options)
    assert verdict.outcome is Outcome.NEEDS_REVIEW
    assert verdict.score <= REVIEW_SCORE_CAP
    assert verdict.summary.startswith(
        f"NEEDS_REVIEW: tamper evidence is required, but tamper detection is {level}: "
    )
    assert verdict.summary.endswith("record a Playwright trace from the harness.")


async def test_required_tamper_evidence_lets_a_trace_pass() -> None:
    verdict = await Judge(SETTINGS).judge(
        _from("playwright-trace"), JudgeOptions(spec=shop_spec(), require_tamper_evidence=True)
    )
    assert verdict.outcome is Outcome.PASS


def test_spec_field_defaults_off_and_loads() -> None:
    assert shop_spec().require_tamper_evidence is False
    data = shop_spec().model_dump() | {"require_tamper_evidence": True}
    assert type(shop_spec()).model_validate(data).require_tamper_evidence is True


async def test_console_prints_the_tamper_warning() -> None:
    verdict = await Judge(SETTINGS).judge(_from("har"), JudgeOptions(spec=shop_spec()))
    console = Console(record=True, width=400)
    print_verdict(verdict, console)
    assert "warning tamper detection is none" in console.export_text()
