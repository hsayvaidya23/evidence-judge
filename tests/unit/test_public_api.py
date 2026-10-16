from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

import evidence_judge

ROOT = Path(__file__).parents[2]
PYTHON_API = (ROOT / "docs" / "reference" / "python-api.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^## [^\n]*{heading}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, heading
    return match.group(1)


def test_every_exported_name_resolves() -> None:
    for name in evidence_judge.__all__:
        assert getattr(evidence_judge, name) is not None, name


def test_exports_are_the_documented_stable_api() -> None:
    rows = re.findall(r"^\| (.+?) \| .+ \|$", _section(PYTHON_API, "Stability"), re.M)
    documented = {name for row in rows[2:] for name in re.findall(r"`([\w.]+)`", row)}
    assert documented == set(evidence_judge.__all__)


def test_exports_include_the_core_workflow() -> None:
    assert {
        "Judge",
        "JudgeOptions",
        "load_run",
        "TaskSpec",
        "RunVerdict",
        "Outcome",
        "Settings",
    } <= set(evidence_judge.__all__)


def test_exports_are_the_same_objects_as_their_internal_homes() -> None:
    from evidence_judge.core.verdict import Outcome, RunVerdict
    from evidence_judge.engine import Judge

    assert evidence_judge.Judge is Judge
    assert evidence_judge.RunVerdict is RunVerdict
    assert evidence_judge.Outcome is Outcome
    from evidence_judge.sessions.judge import SessionJudge

    assert evidence_judge.SessionJudge is SessionJudge


def test_the_documented_session_example_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = ROOT / "tests" / "fixtures" / "sessions" / "claude_code" / "honest_success.jsonl"
    shutil.copy(fixture, tmp_path / "session.jsonl")
    (tmp_path / "session-spec.yaml").write_text("task: fix calc\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("EVJ_CACHE_DIR", str(tmp_path / "cache"))
    example = _python_blocks(_section(PYTHON_API, "Judge a coding-agent session"))[0]
    namespace: dict[str, object] = {}
    exec(compile(example, "<example>", "exec"), namespace)
    assert isinstance(namespace["verdict"], evidence_judge.RunVerdict)


def _python_blocks(text: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", text, re.S)


def test_judge_examples_import_only_from_the_package_root() -> None:
    examples = [
        _python_blocks(_section(README, "Python API"))[0],
        _python_blocks(_section(PYTHON_API, "Judge a run"))[0],
        _python_blocks(_section(PYTHON_API, "Judge a coding-agent session"))[0],
    ]
    for example in examples:
        imports = re.findall(r"^from (\S+) import (\([^)]*\)|.+)$", example, re.M)
        roots = [names for module, names in imports if module == "evidence_judge"]
        assert roots, example
        assert all(module in {"evidence_judge", "asyncio", "pathlib"} for module, _ in imports), (
            imports
        )
        for names in roots:
            for name in re.findall(r"\w+", names):
                assert name in evidence_judge.__all__, name
        compile(example, "<example>", "exec")
