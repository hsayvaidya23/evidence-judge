from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from evidence_judge.core.evidence import Run, Screenshot
from evidence_judge.core.verdict import Outcome
from evidence_judge.report.html_report import MAX_TIMELINE_ROWS, render_html, write_html
from tests.unit.test_report_fixtures import EVIL, PNG_1PX, make_run, make_verdict


@pytest.mark.parametrize(
    ("outcome", "label", "css"),
    [
        (Outcome.PASS, "Pass", "pass"),
        (Outcome.FAIL, "Fail", "fail"),
        (Outcome.NEEDS_REVIEW, "Needs review", "review"),
    ],
)
def test_outcome_banner(outcome: Outcome, label: str, css: str) -> None:
    html = render_html(make_verdict(outcome), make_run())
    assert f'class="banner {css}"' in html
    assert f'<span class="verdict-pill" role="status">{label}</span>' in html
    assert ">42<small>/100</small>" in html
    assert "The order request returned 500" in html


def test_escapes_agent_controlled_text() -> None:
    run = make_run().model_copy(update={"task": EVIL})
    verdict = make_verdict(text=EVIL, quote=f'"><img src=x onerror=alert(1)>{EVIL}').model_copy(
        update={"summary": EVIL, "task": EVIL, "warnings": [EVIL], "run_id": EVIL}
    )
    html = render_html(verdict, run)
    assert "<script>" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;" in html


def test_self_contained_no_external_requests() -> None:
    html = render_html(make_verdict(), make_run())
    assert "Content-Security-Policy" in html
    assert not re.search(r"""(?:src|href)=["']https?://""", html)
    assert "<link" not in html
    assert "<script" not in html


def test_embeds_cited_and_final_screenshots() -> None:
    html = render_html(make_verdict(), make_run())
    b64 = base64.b64encode(PNG_1PX).decode()
    assert html.count(f"data:image/png;base64,{b64}") == 2
    assert 'id="ev-shot-1"' in html and 'id="ev-shot-2"' in html
    assert "Final · shot#2" in html


def test_skips_unsafe_or_missing_images() -> None:
    run = make_run()
    run = run.model_copy(
        update={
            "screenshots": [
                Screenshot(id="shot#1", ts=1, mime_type="text/html", resource="s1.png")
            ],
            "blobs": {},
            "final_state": None,
        }
    )
    html = render_html(make_verdict(), run)
    assert "data:" not in html.replace("img-src data:", "")


def test_claim_cards_and_citations_link_to_timeline() -> None:
    html = render_html(make_verdict(), make_run())
    assert "<blockquote>I placed the order!</blockquote>" in html
    assert "<blockquote>applied a coupon</blockquote>" in html
    assert '<a class="cite" href="#ev-net-2" title="500 Internal Server Error">net#2</a>' in html
    assert 'id="ev-net-2"' in html
    assert "Contradicted" in html and "Supported" in html and "No evidence" in html
    assert ">Key<" in html


def test_timeline_has_actions_and_mutations_only() -> None:
    html = render_html(make_verdict(), make_run())
    assert "POST /api/orders" in html
    assert 'id="ev-net-1"' not in html
    assert 'id="ev-act-2"' in html and "Timeout 5000ms" in html
    assert re.search(r'id="ev-net-2" class="t-bad cited"', html)


def test_timeline_truncates() -> None:
    from evidence_judge.core.evidence import ActionEvent

    run = make_run().model_copy(
        update={
            "actions": [
                ActionEvent(id=f"act#{i}", ts=float(i), name="click")
                for i in range(MAX_TIMELINE_ROWS + 5)
            ]
        }
    )
    html = render_html(make_verdict(), run)
    assert "more not shown" in html


def test_expectations_dimensions_usage() -> None:
    html = render_html(make_verdict(), make_run())
    assert "order_submitted" in html and "Not met" in html and "Not assessable" in html
    assert html.index("Success") < html.index("Side effects") < html.index("State</span>")
    assert "n/a" in html
    assert "1,200" in html and "$0.0123" in html
    assert "HAR had 3 truncated bodies" in html


def test_empty_run_renders() -> None:
    verdict = make_verdict(Outcome.NEEDS_REVIEW).model_copy(
        update={"claims": [], "expectations": [], "dimensions": {}, "warnings": []}
    )
    html = render_html(verdict, Run(run_id="r", source="har"))
    assert "no checkable claims" in html
    assert "No actions or state-changing requests" in html


def test_write_html(tmp_path: Path) -> None:
    out = write_html(make_verdict(), make_run(), tmp_path / "a" / "report.html")
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
