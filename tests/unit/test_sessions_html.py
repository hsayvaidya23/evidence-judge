from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evidence_judge.cli import app
from evidence_judge.core.verdict import RunVerdict, Verification, VerificationMethod
from evidence_judge.sessions.html import render_session_html
from evidence_judge.sessions.ingest import load_session
from evidence_judge.sessions.judge import SessionJudge, SessionJudgeOptions
from evidence_judge.sessions.models import SessionRun
from tests.unit.sessions_helpers import GREEN, RED, SETTINGS, bash, edit, session
from tests.unit.test_report_fixtures import EVIL

FIXTURES = Path(__file__).parent.parent / "fixtures" / "sessions"
SNAPSHOTS = FIXTURES / "html"
runner = CliRunner()


async def _judged(run: SessionRun) -> tuple[RunVerdict, SessionRun]:
    judge = SessionJudge(SETTINGS)
    run, options, _ = judge.redact(run, SessionJudgeOptions(offline=True))
    return await judge.judge(run, options), run


def _sections(html: str) -> str:
    """The session-specific part of the page: the banner's meta, the claims and the timeline."""
    meta = re.search(r'<div class="meta">.*?</div>\n    </div>', html, re.S)
    claims = re.search(r'<section aria-labelledby="claims-h">.*?</section>', html, re.S)
    timeline = re.search(r'<section aria-labelledby="tl-h">.*?</section>', html, re.S)
    assert meta and claims and timeline
    return "\n".join(m.group(0) for m in (meta, claims, timeline)) + "\n"


@pytest.mark.parametrize(
    ("name", "fixture"),
    [
        ("codex_honest_success", FIXTURES / "codex" / "honest_success.exec.jsonl"),
        ("claude_code_honest_failure", FIXTURES / "claude_code" / "honest_failure.jsonl"),
    ],
)
async def test_the_report_matches_its_snapshot(name: str, fixture: Path) -> None:
    if name.startswith("codex"):
        loaded = load_session(codex=fixture)
    else:
        loaded = load_session(claude_code=fixture)
    verdict, run = await _judged(loaded)
    got = _sections(render_session_html(verdict, run))
    snapshot = SNAPSHOTS / f"{name}.html"
    if os.environ.get("EVJ_UPDATE_SNAPSHOTS"):
        snapshot.parent.mkdir(exist_ok=True)
        snapshot.write_text(got, encoding="utf-8")
    assert got == snapshot.read_text(encoding="utf-8"), (
        f"regenerate with EVJ_UPDATE_SNAPSHOTS=1: {snapshot}"
    )


async def test_citations_link_to_the_tool_calls_with_command_and_exit_code() -> None:
    run = session(bash("pytest -q", RED, 1), edit("calc.py"), answer="All tests pass.")
    verdict, run = await _judged(run)
    html = render_session_html(verdict, run)
    assert 'id="ev-cmd-23-1"' in html
    assert 'href="#ev-cmd-23-1"' in html
    assert '<span class="cdesc">pytest -q · exit 1</span>' in html
    assert "<b>transcript: test results come from the output the session recorded</b>" in html
    assert "tool calls in execution order" in html


async def test_a_rerun_verification_is_shown() -> None:
    verdict, run = await _judged(session(edit("calc.py"), bash("pytest", GREEN), answer="Done."))
    rerun = Verification(method=VerificationMethod.RERUN, reason="the judge re-ran pytest")
    html = render_session_html(verdict.model_copy(update={"verification": rerun}), run)
    assert "<b>rerun: the judge re-ran pytest</b>" in html


async def test_agent_text_and_command_output_are_escaped() -> None:
    run = session(
        bash(f"echo '{EVIL}'", f"{EVIL}\n{GREEN}"),
        edit(f"{EVIL}.py"),
        answer=f"All tests pass {EVIL}",
    ).model_copy(update={"task": EVIL})
    verdict, run = await _judged(run)
    html = render_session_html(verdict.model_copy(update={"warnings": [EVIL]}), run)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;" in html


async def test_an_empty_session_renders() -> None:
    verdict, run = await _judged(session())
    assert "The session recorded no tool calls." in render_session_html(verdict, run)


def test_session_judge_writes_a_redacted_html_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("EVJ_REDACT", raising=False)
    monkeypatch.setenv("EVJ_REDACTION_KEY", "test-key")
    secret = "sk-ant-api03-" + "D" * 40
    report = tmp_path / "out" / "report.html"
    result = runner.invoke(
        app,
        [
            "session",
            "judge",
            "--offline",
            "--claude-code",
            str(FIXTURES / "claude_code" / "honest_success.jsonl"),
            "--answer",
            f"All tests pass. Used {secret}.",
            "--html",
            str(report),
        ],
    )
    assert result.exit_code in (0, 2), result.output
    assert "wrote html" in result.output
    html = report.read_text(encoding="utf-8")
    assert secret not in html
    assert '<meta name="generator" content="evidence-judge ' in html
