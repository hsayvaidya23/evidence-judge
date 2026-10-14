from __future__ import annotations

from rich.console import Console

from evidence_judge.core.verdict import Outcome
from evidence_judge.report.console import (
    print_evidence_summary,
    print_verdict,
    summarize_evidence,
)
from tests.unit.test_report_fixtures import make_run, make_verdict


def _render(outcome: Outcome = Outcome.FAIL, text: str = "Ordered the kettle") -> str:
    console = Console(record=True, width=160, color_system=None)
    print_verdict(make_verdict(outcome, text), console)
    return console.export_text()


def test_verdict_output_sections() -> None:
    out = _render()
    assert "FAIL" in out and "score 42/100" in out
    assert "The order request returned 500" in out
    assert "contradicted" in out and "net#2" in out and "[key]" in out
    assert "x order_submitted" in out and "? price" in out
    assert "no_account" not in out
    assert "success 0%" in out and "state n/a" in out
    assert "Order failed" in out
    assert "cost $0.0123" in out
    assert "warning HAR had 3 truncated bodies" in out


def test_agent_text_is_not_rich_markup() -> None:
    out = _render(text="[bold red]pwned[/] [link=http://x]y[/link]")
    assert "[bold red]pwned[/]" in out


def test_needs_review_label() -> None:
    assert "NEEDS REVIEW" in _render(Outcome.NEEDS_REVIEW)


def test_evidence_summary() -> None:
    summary = summarize_evidence(make_run())
    assert summary.counts["actions"] == 2
    assert summary.counts["network"] == 2
    assert summary.counts["mutations"] == 1
    assert [m.id for m in summary.mutations] == ["net#2"]
    assert summary.final_url == "https://shop.test/cart"
    data = summary.to_dict()
    assert data["captured"] == ["actions", "network", "screenshots"]

    console = Console(record=True, width=160, color_system=None)
    print_evidence_summary(summary, console)
    out = console.export_text()
    assert "State-changing requests" in out and "https://shop.test/api/orders" in out
    assert "final https://shop.test/cart  (Cart)" in out
