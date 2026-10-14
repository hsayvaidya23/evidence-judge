from __future__ import annotations

import html
import json
from pathlib import Path

import pytest

from evidence_judge.ci_summary import main, render_summary

HOSTILE = (
    "Done. [click me](http://evil.test/x) ![img](http://evil.test/p.png) "
    '<img src="http://evil.test/t.gif"> <a href="javascript:alert(1)">x</a> '
    "```\n# Heading\n::set-output name=outcome::pass\n::add-mask::secret\n"
    "| a | b |\n</pre><script>alert(1)</script>\n&amp; @team #1"
)


def test_agent_text_cannot_render_markup() -> None:
    out = render_summary({"outcome": "fail", "score": 0.25, "summary": HOSTILE})

    head, _, rest = out.partition("<pre>")
    assert head == "### Evidence Judge: FAIL\n\nScore: 0.25\n\n"
    body, _, tail = rest.partition("</pre>")
    assert tail == "\n"
    for raw in ("<", ">", "`", ":", "[", "]", '"', "'", "\n"):
        assert raw not in body, raw
    assert html.unescape(body) == HOSTILE


def test_no_workflow_command_or_html_survives_anywhere() -> None:
    out = render_summary({"outcome": "fail", "score": 0.1, "summary": HOSTILE})

    assert not any(line.lstrip().startswith("::") for line in out.splitlines())
    assert "<img" not in out and "<script" not in out and "<a " not in out
    assert "javascript:" not in out


@pytest.mark.parametrize(
    ("verdict", "heading", "score"),
    [
        ({"outcome": "pass", "score": 1, "summary": "ok"}, "PASS", "1.00"),
        ({"outcome": "needs_review", "score": 0.5, "summary": ""}, "NEEDS_REVIEW", "0.50"),
        ({"outcome": "<b>pass</b>", "score": "[x](y)", "summary": "s"}, "UNKNOWN", "n/a"),
        ({}, "UNKNOWN", "n/a"),
    ],
)
def test_outcome_and_score_are_validated(
    verdict: dict[str, object], heading: str, score: str
) -> None:
    out = render_summary(verdict)
    assert out.startswith(f"### Evidence Judge: {heading}\n\nScore: {score}\n\n<pre>")


def test_main_appends_summary_and_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({"outcome": "fail", "score": 0.2, "summary": HOSTILE}))
    summary = tmp_path / "summary.md"
    outputs = tmp_path / "outputs"
    summary.write_text("earlier\n")

    code = main(
        [
            str(verdict),
            "--summary",
            str(summary),
            "--outputs",
            str(outputs),
            "--report",
            "/r/report.html",
        ]
    )

    assert code == 0
    assert capsys.readouterr().out == "fail\n"
    assert summary.read_text().startswith("earlier\n### Evidence Judge: FAIL")
    assert outputs.read_text() == "outcome=fail\nscore=0.2\nreport=/r/report.html\n"


def test_main_reads_github_files_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({"outcome": "pass", "score": 0.9, "summary": "fine"}))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "s.md"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))

    assert main([str(verdict)]) == 0

    assert capsys.readouterr().out == "pass\n"
    assert "<pre>fine</pre>" in (tmp_path / "s.md").read_text()
    assert (tmp_path / "o").read_text() == "outcome=pass\nscore=0.9\n"


def test_main_rejects_an_unreadable_verdict(tmp_path: Path) -> None:
    bad = tmp_path / "verdict.json"
    bad.write_text("not json")

    assert main([str(bad), "--summary", str(tmp_path / "s"), "--outputs", str(tmp_path / "o")]) == 3
